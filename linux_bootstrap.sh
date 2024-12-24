#!/bin/bash

REGULAR_USER=${SUDO_USER:-${USER}}
REGULAR_USER_HOME=/home/${SUDO_USER:-${USER}}
SSH_DIR=/home/${REGULAR_USER}/.ssh
SSH_AUTH=$SSH_DIR/authorized_keys

# TODO:
# -- Check if run as Root

echo -e "\n--Bootstrapping User Account ${SUDO_USER:-${HOSTNAME}}..."

# First add  USER keys.
echo -e "\n--Check for Local user SSH directory setup--"
if [ ! -d ${SSH_DIR} ]; then
	echo -e "\n---- $SSH_DIR does not exist, creating it..."
	mkdir $SSH_DIR;
	chmod 700 $SSH_DIR
 	chown ${REGULAR_USER}:${REGULAR_USER} ${SSH_DIR}
else
 	echo -e "\n--Checking for SSH keys in $SSH_AUTH"
	if [ -f "$SSH_AUTH" ] && test "grep work $SSH_AUTH" == 0; then
		echo '----No need to setup SSH keys'
	else
		echo -e "\n----Setting up SSH keys for ${REGULAR_USER}..."
		touch $SSH_AUTH
 		chown ${REGULAR_USER}:${REGULAR_USER} ${SSH_AUTH}
		cat << EOF > $SSH_AUTH
ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDXP7oE/7jhnxcQXNVYzTC0ZbtHV2m9sMin7rSel+byUw3jDss5FwpSkjD8/2NKxojjsONybyC0DHNB8pzhuu/oMJuwR/s48t77cW305TfR7z4uwlim1I0BlX7u8oPop1DhFG/M2H6Gequ8Wi2FtlSvmDlclUgireIpHQypgG/8AL8BxujxNZVeK0t9yHDIXESw/btii45KzqXsU3P21zGzBNB4ZR145wcL+/J/lAlRBwD5ex9B08JJvatyLFlTZXOo0gHqO25+tkVLgaWI9Ou7Q5TgrWuFPNJb+M5/kgni0YokzwZ0pG06G4Fk+d9zGT4rv/8RaxKVt3f5czkQRVIp emanners@work_ubuntu
EOF
		echo -e "\n----Keys added to ${SSH_AUTH}"
	fi
fi

# Also add ROOT SSH keys
echo -e "\n--Check for Root User SSH directory setup--"
if [ ! -d /root/.ssh ]; then
	mkdir /root/.ssh;
	chmod 700 /root/.ssh
else
 	echo -e "\n--Checking for SSH keys in /root/.ssh/authorized_keys"
	if [ -f "/root/.ssh/authorized_keys" ] && test "grep foreman /root/.ssh/authorized_keys" == 0; then
		echo -e '----No need to setup Root SSH keys'
	else
		echo -e "\n----Setting up Root SSH keys..."
		touch /root/.ssh/authorized_keys
 		chown root:root /root/.ssh/authorized_keys
		cat << EOF > /root/.ssh/authorized_keys
ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCcGnCtPnIkI/dTA6lJHKaFskgoofFdGFbUPwMAOqp4R35BqCGtc2gcOjbCwyQjfirHzIK7r/HyKOyVp5Oz2p6bJybhaO+G2uSWvtWeXuOCs9qX4BpHt3t96eRKsqD6tN6s/fMx4knc3DbygNp14pShfevZkAhYHhoDDATvaHZuatRgJY8Oq+NumOwQcNSlbIA5jBes2lnVPAXyhai1OMShIuWpugs3Pht70G6zw43NVAqFWdSvOJRJ9QffUPuzFdvAfrai0RQuul4W9LIl0TwiUC9/tMSngCBuA8cJIBUe3WeTBaPbNAocBvTzoiFYVXpjHPJAfsCyIk9aUv2143lpv7a8euHgkdvrp5YQ8nkXzS7si7jhVs7Ycp6HW/O0npUW5s3KeE0943pclinmEwp+U+BqmX+MHl3TZCTsdhgWQFZbPNfG9aFhEIgYXjBE9kdlQQgN/dN3FDAhCswKzvFzuJg7PL00gkzqeo9CAUr/WZfM4obbTRg0pVjt9NT/9n8= foreman-proxy@katello.edsonmanners.com
EOF
		echo -e "\n----Keys added to /root/.ssh"
	fi
fi

OS=$(grep ID_LIKE /etc/os-release | grep rhel | cut -d '=' -f 2 | sed s/\"//g | awk '{ print $1 }')
if [ $OS == 'rhel' ]; then
  echo -e "\n--Update packages\n"
  sudo dnf -y update
else
  echo -e "\n--Updating apt cache and running apt upgrade\n"
  sudo apt update
  sudo apt -y upgrade
fi

BIN='/usr/bin'
USEFUL_APPS='vim git screen curl'
for i in ${USEFUL_APPS};
  do if [ ! -f "{BIN}/${i}" ]; then
    echo -e "\n--Adding useful app... ${i}"
    sudo apt -y install ${i}
  else
    echo -e "\n--${i} already installed..."
  fi
done


echo -e "\n--Initialize GIT environment"
if [ -d "${REGULAR_USER_HOME}/Code" ]; then
	echo "----No need to setup ${REGULAR_USER_HOME}, it already exists"
else
	echo "--Creating Code directory: ${REGULAR_USER_HOME}/Code"
	sudo --user=${REGULAR_USER} mkdir ${REGULAR_USER_HOME}/Code
	cd ${REGULAR_USER_HOME}/Code
	if [ -f "${REGULAR_USER}/Code/Robotics" ]; then
		echo '--Git environment already setup'
	else
		sudo --user=${REGULAR_USER} git clone https://github.com/emannersvip/Robotics.git 
	fi
fi

# SSH Keys in order to make GitHUB changes?
#if test -f "${SSH_DIR}/id_ecdsa"; then
#	echo '--It looks like SSH keys are already in place' 
#	if test -f "${SSH_DIR}/config"; then
#		echo '----SSH config is also in place. Good!' 
#	else
#		cat << EOF > ${SSH_DIR}/config
#Host github.com
#AddKeysToAgent yes
#IdentityFile ~/.ssh/id_ecdsa
#EOF
#	fi
#else
#	echo '--It looks like SSH keys are *NOT* setup. Copy SSH keys at first convenience'
#fi

# Stop uneeded services
UNNEEDED_SVCS='avahi-daemon cups bluetooth'
sudo systemctl stop ${UNNEEDED_SVCS}
sudo systemctl disable ${UNNEEDED_SVCS}

echo -e "\n\n"

