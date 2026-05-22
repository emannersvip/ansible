# Ansible Master Role
* Right click the **'Raw'** button to get the URL for the non-HTML link.

```bash
sudo /bin/bash -c "$(curl -fsSL https://github.com/emannersvip/ansible/raw/refs/heads/main/linux_bootstrap.sh)"
```

* Run Ansible role-based playbook
```bash
ansible-playbook -vvv -k -i inventory.yml prometheus.yml
```

* Create new Ansible role
```bash
ansible-galaxy init roles/new-role
```
