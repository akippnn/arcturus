#define _GNU_SOURCE
#include <linux/reboot.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <stdio.h>
int main(void) {
    sync();
    syscall(SYS_reboot, LINUX_REBOOT_MAGIC1, LINUX_REBOOT_MAGIC2,
            LINUX_REBOOT_CMD_RESTART2, "0 tryboot");
    perror("tryboot reboot");
    return 1;
}
