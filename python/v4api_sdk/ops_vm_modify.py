import getpass
import argparse
import sys
import uuid
import urllib3

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

# Utility functions for handling responses and tasks
from tme.utils import Utils

def main():
    # Disable SSL warnings so the terminal looks clean
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # These are the arguments that must be passed when running the script
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

    # Ask user to type Prism Central password safely
    cluster_password = getpass.getpass(prompt="Enter your Prism Central password: ", stream=None)

    # If nothing entered, keep asking again
    if not cluster_password:
        while not cluster_password:
            print("Password cannot be empty. Try again.")
            cluster_password = getpass.getpass(prompt="Enter your Prism Central password: ", stream=None)

    try:
        # Connect to Prism Central using provided credentials
        utils = Utils(pc_ip=args.pc_ip, username=args.username, password=cluster_password)

        # Prepare both Prism and VMM configs for API access
        prism_config = PrismConfiguration()
        vmm_config = VMMConfiguration()

        for config in [prism_config, vmm_config]:
            config.host = args.pc_ip
            config.username = args.username
            config.password = cluster_password
            config.verify_ssl = False

        # Use VMM API to list VMs
        vmm_client = VMMClient(configuration=vmm_config)
        vmm_instance = ntnx_vmm_py_client.api.VmApi(api_client=vmm_client)

        # Get all VMs starting with a specific name prefix (e.g. demo)
        vm_list = vmm_instance.list_vms(async_req=False, _filter=f"startswith(name, '{args.prefix}')")

        if not vm_list.data:
            print("No matching VMs found.")
            sys.exit(0)

        # Prepare Prism client for batch update
        prism_client = PrismClient(configuration=prism_config)
        prism_client.add_default_header("Accept-Encoding", "gzip, deflate, br")
        batch_instance = ntnx_prism_py_client.api.BatchesApi(api_client=prism_client)

        # This is used to generate a unique task name
        unique_id = uuid.uuid1()
        batch_spec_payload_list = []

        # Predefined VM sizes: what CPU, RAM, and disk each size gets
        size_presets = {
            "small": {"cpu": 4, "memory": 8, "disk": 100},
            "medium": {"cpu": 8, "memory": 16, "disk": 200},
            "large": {"cpu": 12, "memory": 32, "disk": 300}
        }

        # Each department is mapped to exactly one VLAN UUID
        vlan_uuid_map = {
            "nutanix": "36e4493e-f072-4ba2-b299-edc65993eb5e",
            "openshift": "53986909-0aef-443e-a6ca-2c1057b65306",
            "ansible": "ed6ad55b-69f5-470d-83b2-4ed55b6413a0"
        }

        # Pick the resource settings for the size user selected
        selected_config = size_presets[args.size]

        # Get the VLAN UUID for the selected department
        try:
            selected_vlan_uuid = vlan_uuid_map[args.department]
        except KeyError:
            print("Invalid department. Please check VLAN mappings.")
            sys.exit(1)

        # Go through all matched VMs and build their updated config
        for vm in vm_list.data:
            vm_data = vmm_instance.get_vm_by_id(vm.ext_id).data

            # Set CPU and memory values vm sizing logic
            vm_data.num_vcpus = selected_config["cpu"]
            vm_data.memory_size_mib = selected_config["memory"] * 1024

            # Update the VLAN for the first NIC
            if vm_data.nic_list:
                vm_data.nic_list[0].subnet_reference.uuid = selected_vlan_uuid

            # Get version token to support batch update
            etag = vmm_client.get_etag(vm_data)

            # Add this VM to the update list
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

        # Build the final batch update request
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

        print("Submitting batch to update VM CPU, memory and VLAN...")

        # Send the batch update request
        batch_response = batch_instance.submit_batch(async_req=False, body=batch_spec)

        # Track the task status until it finishes
        modify_ext_id = batch_response.data.ext_id

        utils.monitor_task(
            task_ext_id=modify_ext_id,
            task_name="Batch VM Resource & VLAN Update",
            pc_ip=args.pc_ip,
            username=args.username,
            password=cluster_password,
            poll_timeout=args.poll
        )

        print(f"Updated {len(batch_spec_payload_list)} VM(s).")

    except VMMException as e:
        print(f"Error during VM update: {e}")


if _name_ == "_main_":
    main()