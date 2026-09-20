nfs_client
==========

Mounts the infra01 Ganesha export (`infra01.edsonmanners.com:/extHD`) on Swarm
nodes and creates a Docker named volume (`nfs-dockerswarm`) that points at
`/extHD/DockerSwarm`.

Use this so Swarm services can schedule on any node in `vm_compute` and still
see the same data.

Requirements
------------

- `nfs-ganesha` running on infra01 with `Path = /mnt/extHD` and `Pseudo = /extHD`
- `/mnt/extHD` must be mounted **before** Ganesha starts (RequiresMountsFor)
- `community.docker` and `ansible.posix` collections
- Docker Engine already installed (see `docker_server`)

Role Variables
--------------

See `defaults/main.yml`. Override `nfs_client_docker_volume_name` or
`nfs_client_docker_device` if you need extra volumes later.

Example Playbook
----------------

    - hosts: vm_compute
      become: true
      roles:
        - nfs_client

Attach the volume on a service with:

    docker service create \
      --name example \
      --mount type=volume,source=nfs-dockerswarm,target=/data \
      alpine sleep infinity

Ganesha notes
-------------

NFSv3 mountd is not usable on this export (`showmount` fails). Clients must
use NFSv4 (`vers=4.2` / `nfsvers=4.2`). Do not put `clientaddr=` in fstab.
