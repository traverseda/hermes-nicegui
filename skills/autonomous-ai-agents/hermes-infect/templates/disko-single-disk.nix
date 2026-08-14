# Single-disk GPT disko layout for nixos-anywhere (EFI + ext4 root).
#
# Copy into the friend host config (or import it) and CHANGE `device` to the
# target's real disk — `lsblk` on the machine to find it (/dev/sda on most
# VPS, /dev/nvme0n1 on NVMe, /dev/vda on KVM).
#
# This is the minimal layout that boots on UEFI hardware. Adjust to taste
# (swap partition, btrfs, LUKS via --disk-encryption-keys) per the machine.

{
  disko.devices.disk.main = {
    type = "disk";
    device = "/dev/sda";
    content = {
      type = "gpt";
      partitions = {
        ESP = {
          size = "512M";
          type = "EF00";
          content = {
            type = "filesystem";
            format = "vfat";
            mountpoint = "/boot";
            mountOptions = [ "umask=0077" ];
          };
        };
        root = {
          size = "100%";
          content = {
            type = "filesystem";
            format = "ext4";
            mountpoint = "/";
          };
        };
      };
    };
  };
}
