/*
 * vulnerable.c — Buffer overflow test target for exploit development pipeline.
 *
 * Compile:
 *   gcc -fno-stack-protector -z execstack -no-pie -o vulnerable vulnerable.c
 *
 * Mitigations OFF: No Canary, NX disabled, No PIE.
 * This makes it trivially exploitable for testing the feedback loop.
 *
 * Usage: ./vulnerable <input_string>
 * Overflow at 64 bytes overwrites return address.
 */

#include <stdio.h>
#include <string.h>
#include <unistd.h>

void vuln(char *input) {
    char buf[64];
    strcpy(buf, input);  // Classic buffer overflow — no bounds check
    printf("Received: %s\n", buf);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <input>\n", argv[0]);
        return 1;
    }
    printf("PID: %d\n", getpid());
    vuln(argv[1]);
    return 0;
}
