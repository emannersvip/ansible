# Ansible Master Role
---
[MarkDown Examples](https://github.com/colbymchenry/codegraph/blob/main/README.md?plain=1)

* Be sure to run the command below as `emanners` and **not** the `root` user.

```bash
~~sudo /bin/bash -c "$(curl -fsSL https://github.com/emannersvip/ansible/raw/refs/heads/main/linux_bootstrap.sh)"~~
curl -fsSL https://github.com/emannersvip/ansible/raw/refs/heads/main/linux_bootstrap.sh | sudo bash
```

* Run Ansible role-based playbook
```bash
ansible-playbook -vvv -k --diff --check --ask-become -i inventory.yml prometheus.yml
```

* Create new Ansible role
```bash
ansible-galaxy init roles/new-role
```
