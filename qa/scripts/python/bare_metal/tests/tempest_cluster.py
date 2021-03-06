import sys
import time
import argparse
from pprint import pprint
from string import Template
from novaclient.v1_1 import client
from rpcsqa_helper import rpcsqa_helper

# Parse arguments from the cmd line
parser = argparse.ArgumentParser()
parser.add_argument('--name', action="store", dest="name",
                    required=False, default="test",
                    help="Name for the openstack chef environment")
parser.add_argument('--os_distro', action="store", dest="os_distro",
                    required=False, default='precise',
                    help="Operating System to use for openstack")
parser.add_argument('--feature_set', action="store", dest="feature_set",
                    required=False, default='default',
                    help="Openstack feature set to use")
parser.add_argument('--environment_branch', action="store",
                    dest="environment_branch",
                    required=False,
                    default="folsom")
parser.add_argument('--tempest_version', action="store",
                    dest="tempest_version", required=False,
                    default="grizzly")
parser.add_argument('--keystone_admin_pass', action="store",
                    dest="keystone_admin_pass", required=False,
                    default="ostackdemo")
results = parser.parse_args()

# Get cluster's environment
qa = rpcsqa_helper()
env_dict = {"name": results.name,
            "os_distro": results.os_distro,
            "feature_set": results.feature_set,
            "branch": results.environment_branch}
local_env = qa.cluster_environment(**env_dict)
if not local_env.exists:
    print "Error: Environment %s doesn't exist" % local_env.name
    sys.exit(1)
remote_chef = qa.remote_chef_api(local_env)
env = qa.cluster_environment(chef_api=remote_chef, **env_dict)

# Gather information from the cluster
controller, ip = qa.cluster_controller(env, remote_chef)
if not controller:
    print "Controller not found for env: %s" % env.name
    sys.exit(1)
username = 'demo'
password = results.keystone_admin_pass
tenant = 'demo'

cluster = {
    'host': ip,
    'username': username,
    'password': password,
    'tenant': tenant,
    'alt_username': username,
    'alt_password': password,
    'alt_tenant': tenant,
    'admin_username': "admin",
    'admin_password': password,
    'admin_tenant': "admin",
    'nova_password': controller.attributes['nova']['db']['password']
}
if results.tempest_version == 'grizzly':
    # quantum is enabled, test it.
    if 'nova-quantum' in results.feature_set:
        cluster['api_version'] = 'v2.0'
        cluster['tenant_network_cidr'] = '10.0.0.128/25'
        cluster['tenant_network_mask_bits'] = '25'
        cluster['tenant_networks_reachable'] = True
        cluster['public_router_id'] = ''
        cluster['public_network_id'] = ''
        cluster['quantum_available'] = True
    else:
        cluster['api_version'] = 'v1.1'
        cluster['tenant_network_cidr'] = '10.100.0.0/16'
        cluster['tenant_network_mask_bits'] = '29'
        cluster['tenant_networks_reachable'] = False
        cluster['public_router_id'] = ''
        cluster['public_network_id'] = ''
        cluster['quantum_available'] = False

if results.feature_set == "glance-cf":
    cluster["image_enabled"] = True
else:
    cluster["image_enabled"] = False

# Getting precise image id
url = "http://%s:5000/v2.0" % ip
print "##### URL: %s #####" % url
compute = client.Client(username,
                        password,
                        tenant,
                        url,
                        service_type="compute")
precise_id = (i.id for i in compute.images.list() if i.name == "precise-image")
cluster['image_id'] = next(precise_id)
cluster['alt_image_id'] = cluster['image_id']

pprint(cluster)

# Write the config
tempest_dir = "/var/lib/jenkins/jenkins-build/qa/metadata/tempest/config"
sample_path = "%s/base_%s.conf" % (tempest_dir, results.tempest_version)
with open(sample_path) as f:
    tempest_config = Template(f.read()).substitute(cluster)
tempest_config_path = "/tmp/%s.conf" % env.name
with open(tempest_config_path, 'w') as w:
    print "####### Tempest Config #######"
    print tempest_config_path
    print tempest_config
    w.write(tempest_config)
qa.scp_to_node(node=controller, path=tempest_config_path)

# Setup tempest on chef server
print "## Setting up tempest on chef server ##"
if results.os_distro == "precise":
    packages = "apt-get install python-pip libmysqlclient-dev libxml2-dev libxslt1-dev python2.7-dev libpq-dev git -y"
else:
    packages = "yum install python-pip python-lxml gcc python-devel openssl-devel mysql-devel postgresql-devel git -y; easy_install pip"
commands = [packages,
            "rm -rf tempest",
            "git clone https://github.com/openstack/tempest.git -b stable/%s --recursive" % (results.tempest_version),
            "easy_install -U distribute",
            "pip install -r tempest/tools/pip-requires",
            "pip install -r tempest/tools/test-requires"]
for command in commands:
    qa.run_cmd_on_node(node=controller, cmd=command)

# Setup controller
print "## Setting up and cleaning cluster ##"
setup_cmd = ("sysctl -w net.ipv4.ip_forward=1; "
             "source ~/openrc; "
             "nova-manage floating list | grep eth0 > /dev/null || nova-manage floating create 192.168.2.0/24; "
             "nova-manage floating list;")
qa.run_cmd_on_node(node=controller, cmd=setup_cmd)

# Run tests
print "## Running Tests ##"

file = '%s-%s.xunit' % (
    time.strftime("%Y-%m-%d-%H:%M:%S",
                  time.gmtime()),
    env.name)
xunit_flag = '--with-xunit --xunit-file=%s' % file

exclude_flags = ["volume", "rescue"]  # Volumes
if results.feature_set != "glance-cf":
    exclude_flags.append("image")
exclude_flag = ' '.join('-e {0}'.format(x) for x in exclude_flags)

command = ("export TEMPEST_CONFIG_DIR=/root; "
           "export TEMPEST_CONFIG=%s.conf; "
           "python -u `which nosetests` %s %s tempest; " % (
               env.name, xunit_flag, exclude_flag))
qa.run_cmd_on_node(node=controller, cmd=command)

# Transfer xunit file to jenkins workspace
print "## Transfering xunit file ##"
qa.scp_from_node(node=controller, path=file, destination=".")
