/*
 * vuln_c_demo.c — Demonstrates multiple C vulnerability classes.
 * DO NOT COMPILE FOR PRODUCTION. Test target for c_analyzer.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define BUFSIZE 64

/* Buffer overflow: unbounded strcpy */
void unsafe_copy(char *input) {
    char buf[BUFSIZE];
    strcpy(buf, input);  // VULN: no bounds check
    printf("Copied: %s\n", buf);
}

/* Format string: user-controlled format */
void unsafe_print(char *input) {
    printf(input);  // VULN: format string, user controls format
}

/* Use-after-free */
void use_after_free(char *data) {
    char *ptr = strdup(data);
    free(ptr);
    printf("%s\n", ptr);  // VULN: use after free
}

/* Double free */
void double_free(char *data) {
    char *ptr = strdup(data);
    free(ptr);
    free(ptr);  // VULN: double free
}

/* Integer overflow in allocation */
void *unsafe_alloc(size_t count, size_t size) {
    return malloc(count * size);  // VULN: count*size can overflow
}

/* Null dereference */
int null_deref(char *maybe_null, int flag) {
    if (flag) {
        maybe_null = NULL;
    }
    return *maybe_null;  // VULN: may be null depending on flag
}

/* Unsafe gets() — always a vuln */
void unsafe_gets() {
    char buf[BUFSIZE];
    gets(buf);  // VULN: gets() has no bounds whatsoever
    printf("Got: %s\n", buf);
}

int main(int argc, char **argv) {
    if (argc < 2) return 1;
    unsafe_copy(argv[1]);
    unsafe_print(argv[1]);
    use_after_free(argv[1]);
    double_free(argv[1]);
    unsafe_gets();
    return 0;
}
