import getpass
import argparse
import sys
import uuid
import urllib3
import time

# Import Nutanix VMM client libraries
import ntnx_vmm_py_client
from ntnx_vmm_py_client import Configuration as VMMConfiguration
from ntnx_vmm_py_client import ApiClient as VMMClient
from ntnx_vmm_py_client.rest import ApiException as VMMException

# Import Nutanix Prism Central client libraries
import ntnx_prism_py_client
from ntnx_prism_py_client import Configuration as PrismConfiguration
from ntnx_prism_py_client import ApiClient as PrismClient

# Batch update request model imports
from ntnx_prism_py_client.models.prism.v4.operations.BatchSpec import BatchSpec
from ntnx_prism_py_client.models.prism.v4.operations.BatchSpecMetadata import BatchSpecMetadata
from ntnx_prism_py_client.models.prism.v4.operations.BatchSpecPayload import BatchSpecPayload
from ntnx_prism_py_client.models.prism.v4.operations.BatchSpecPayloadMetadata import BatchSpecPayloadMetadata
from ntnx_prism_py_client.models.prism.v4.operations.BatchSpecPayloadMetadataHeader import BatchSpecPayloadMetadataHeader
from ntnx_prism_py_client.models.prism.v4.operations.BatchSpecPayloadMetadataPath import BatchSpecPayloadMetadataPath
from ntnx_prism_py_client.models.prism.v4.operations.ActionType import ActionType

from ntnx_prism_py_client.api.tasks_api import TasksApi

def main():
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    parser = argparse.ArgumentParser()
    parser.add_argument("pc_ip", help="Prism Central IP address or FQDN")
    parser.add_argument("username", help="Prism Central username")
    parser.add_argument("--size", choices=["small", "medium", "large"], required=True,
                        help="Choose VM size: small / medium / large")
    parser.add_argument("--department", choices=["nutanix", "openshift", "ansible"], required=True,
                        help="Department name to map VLAN")
    parser.add_argument("--prefix", type=str, default="batchdemo", help="VM name prefix to filter")
    parser.add_argument("-p", "--poll", help="Time between task polling, in seconds", default=1)
    args = parser.parse_args()

    cluster_password = getpass.getpass(prompt="Enter your Prism Central password: ", stream=None)

    if not cluster_password:
        while not cluster_password:
            print("Password cannot be empty. Try again.")
            cluster_password = getpass.getpass(prompt="Enter your Prism Central password: ", stream=None)

    try:
        prism_config = PrismConfiguration()
        vmm_config = VMMConfiguration()

        for config in [prism_config, vmm_config]:
            config.host = args.pc_ip
            config.username = args.username
            config.password = cluster_password
            config.verify_ssl = False

        vmm_client = VMMClient(configuration=vmm_config)
        vmm_instance = ntnx_vmm_py_client.api.VmApi(api_client=vmm_client)

        vm_list = vmm_instance.list_vms(async_req=False, _filter=f"startswith(name, '{args.prefix}')")

        if not vm_list.data:
            print("No matching VMs found.")
            sys.exit(0)

        prism_client = PrismClient(configuration=prism_config)
        prism_client.add_default_header("Accept-Encoding", "gzip, deflate, br")
        batch_instance = ntnx_prism_py_client.api.BatchesApi(api_client=prism_client)
        tasks_api = TasksApi(api_client=prism_client)

        unique_id = uuid.uuid1()
        batch_spec_payload_list = []

        size_presets = {
            "small": {"cpu": 4, "memory": 8, "disk": 100},
            "medium": {"cpu": 8, "memory": 16, "disk": 200},
            "large": {"cpu": 12, "memory": 32, "disk": 300}
        }

        vlan_uuid_map = {
            "nutanix": "36e4493e-f072-4ba2-b299-edc65993eb5e",
            "openshift": "53986909-0aef-443e-a6ca-2c1057b65306",
            "ansible": "ed6ad55b-69f5-470d-83b2-4ed55b6413a0"
        }

        selected_config = size_presets[args.size]

        try:
            selected_vlan_uuid = vlan_uuid_map[args.department]
        except KeyError:
            print("Invalid department. Please check VLAN mappings.")
            sys.exit(1)

        for vm in vm_list.data:
            vm_data = vmm_instance.get_vm_by_id(vm.ext_id).data
            vm_data.num_vcpus = selected_config["cpu"]
            vm_data.memory_size_mib = selected_config["memory"] * 1024
            
            if vm_data.disk_list:
                vm_data.disk_list[0].disk_size_mib = selected_config["disk"] * 1024

            if vm_data.nic_list:
                vm_data.nic_list[0].subnet_reference.uuid = selected_vlan_uuid

            etag = vmm_client.get_etag(vm_data)

            batch_spec_payload_list.append(
                BatchSpecPayload(
                    data=vm_data,
                    metadata=BatchSpecPayloadMetadata(
                        headers=[
                            BatchSpecPayloadMetadataHeader(name="If-Match", value=etag)
                        ],
                        path=[
                            BatchSpecPayloadMetadataPath(name="extId", value=vm_data.ext_id)
                        ]
                    )
                )
            )

        batch_spec = BatchSpec(
            metadata=BatchSpecMetadata(
                action=ActionType.MODIFY,
                name=f"update_{unique_id}",
                uri="/api/vmm/v4.0.b1/ahv/config/vms/{extId}",
                stop_on_error=True,
                chunk_size=1
            ),
            payload=batch_spec_payload_list
        )

        print("Submitting batch to update VM CPU, memory, disk and VLAN...")

        batch_response = batch_instance.submit_batch(async_req=False, body=batch_spec)
        modify_ext_id = batch_response.data.ext_id

        print("Tracking task status...")
        while True:
            task = tasks_api.get_task(modify_ext_id).data
            state = task.status.state

            if state == "SUCCEEDED":
                print("Task completed successfully.")
                break
            elif state == "FAILED":
                print("Task failed.")
                break
            else:
                print(f" Task in progress... Status: {state}")
                time.sleep(int(args.poll))

        print(f"Updated {len(batch_spec_payload_list)} VM(s).")

    except VMMException as e:
        print(f"Error during VM update: {e}")

if _name_ == "_main_":
    main()
