# Ansible
# Right click the 'Raw' button to get the URL for the non-HTML link.
sudo /bin/bash -c "$(curl -fsSL https://github.com/emannersvip/ansible/raw/refs/heads/main/linux_bootstrap.sh)"

# Run Ansible role-based playbook
ansible-playbook -vvv -k -i inventory.yml prometheus.yml
