#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <linux/input-event-codes.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <wayland-client.h>

#include "virtual-pointer-client.h"

static volatile sig_atomic_t cancelled;

static void cancel_drag(int signal_number) {
    (void)signal_number;
    cancelled = 1;
}

static uint64_t monotonic_ms(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * 1000 + (uint64_t)now.tv_nsec / 1000000;
}

static void got_sync(void *data, struct wl_callback *callback, uint32_t serial) {
    (void)serial;
    *(bool *)data = true;
    wl_callback_destroy(callback);
}

static const struct wl_callback_listener SYNC_LISTENER = {.done = got_sync};

static bool sync_display(struct wl_display *display) {
    bool done = false;
    struct wl_callback *callback = wl_display_sync(display);
    const uint64_t deadline = monotonic_ms() + 1000;
    if (!callback)
        return false;
    wl_callback_add_listener(callback, &SYNC_LISTENER, &done);
    while (!done) {
        if (wl_display_dispatch_pending(display) < 0 || done)
            break;
        struct pollfd read_fd = {.fd = wl_display_get_fd(display), .events = POLLIN};
        if (wl_display_flush(display) < 0) {
            if (errno != EAGAIN)
                break;
            read_fd.events |= POLLOUT;
        }
        uint64_t now = monotonic_ms();
        if (now >= deadline)
            break;
        int ready = poll(&read_fd, 1, (int)(deadline - now));
        if (ready < 0 && errno == EINTR)
            continue;
        if (ready <= 0 || (read_fd.revents & (POLLERR | POLLHUP | POLLNVAL)))
            break;
        if ((read_fd.revents & POLLIN) && wl_display_dispatch(display) < 0)
            break;
    }
    if (!done)
        wl_callback_destroy(callback);
    return done;
}

static void got_global(void *data, struct wl_registry *registry, uint32_t id,
                       const char *interface, uint32_t version) {
    (void)version;
    struct zwlr_virtual_pointer_manager_v1 **manager = data;
    if (!*manager && !strcmp(interface, zwlr_virtual_pointer_manager_v1_interface.name))
        *manager = wl_registry_bind(registry, id, &zwlr_virtual_pointer_manager_v1_interface, 1);
}

static void lost_global(void *data, struct wl_registry *registry, uint32_t id) {
    (void)data;
    (void)registry;
    (void)id;
}

static const struct wl_registry_listener REGISTRY_LISTENER = {
    .global = got_global,
    .global_remove = lost_global,
};

static bool number(const char *text, uint32_t *value) {
    if (!*text || strspn(text, "0123456789") != strlen(text))
        return false;
    errno = 0;
    unsigned long parsed = strtoul(text, NULL, 10);
    if (errno || parsed > UINT32_MAX)
        return false;
    *value = (uint32_t)parsed;
    return true;
}

static void move(struct zwlr_virtual_pointer_v1 *pointer, uint32_t x, uint32_t y,
                 uint32_t width, uint32_t height) {
    zwlr_virtual_pointer_v1_motion_absolute(pointer, (uint32_t)monotonic_ms(), x, y, width, height);
    zwlr_virtual_pointer_v1_frame(pointer);
}

static void button(struct zwlr_virtual_pointer_v1 *pointer, enum wl_pointer_button_state state) {
    zwlr_virtual_pointer_v1_button(pointer, (uint32_t)monotonic_ms(), BTN_LEFT, state);
    zwlr_virtual_pointer_v1_frame(pointer);
}

int main(int argc, char **argv) {
    uint32_t coords[6];
    bool move_only = argc == 6 && !strcmp(argv[1], "--move");
    if (argc != 7 && !move_only) {
        fputs("Usage: drag x1 y1 x2 y2 width height | drag --move x y width height\n", stderr);
        return 2;
    }
    for (int i = 0; i < (move_only ? 4 : 6); ++i) {
        if (!number(argv[i + (move_only ? 2 : 1)], &coords[i])) {
            fputs("Pointer coordinates must be unsigned integers.\n", stderr);
            return 2;
        }
    }
    const uint32_t x1 = coords[0], y1 = coords[1];
    const uint32_t x2 = move_only ? x1 : coords[2], y2 = move_only ? y1 : coords[3];
    const uint32_t width = coords[move_only ? 2 : 4], height = coords[move_only ? 3 : 5];
    if (x1 >= width || x2 >= width || y1 >= height || y2 >= height) {
        fputs("Pointer coordinates exceed the desktop bounds.\n", stderr);
        return 2;
    }

    struct sigaction stop = {.sa_handler = cancel_drag};
    sigemptyset(&stop.sa_mask);
    if (sigaction(SIGTERM, &stop, NULL) || sigaction(SIGINT, &stop, NULL) ||
        sigaction(SIGHUP, &stop, NULL)) {
        fputs("Could not set drag cancellation handlers.\n", stderr);
        return 1;
    }
    struct wl_display *display = wl_display_connect(NULL);
    if (!display) {
        fputs("Could not connect to the Wayland display.\n", stderr);
        return 1;
    }
    struct wl_registry *registry = wl_display_get_registry(display);
    struct zwlr_virtual_pointer_manager_v1 *manager = NULL;
    struct zwlr_virtual_pointer_v1 *pointer = NULL;
    int result = 1;
    if (!registry || wl_registry_add_listener(registry, &REGISTRY_LISTENER, &manager) < 0 ||
        !sync_display(display) || !manager) {
        fputs("Wayland virtual pointer is unavailable.\n", stderr);
        goto cleanup;
    }
    pointer = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(manager, NULL);
    if (!pointer || !sync_display(display)) {
        fputs("Could not create the Wayland virtual pointer.\n", stderr);
        goto cleanup;
    }
    if (cancelled)
        goto cleanup;
    move(pointer, x1, y1, width, height);
    if (move_only) {
        if (sync_display(display) && !cancelled)
            result = 0;
        goto cleanup;
    }
    button(pointer, WL_POINTER_BUTTON_STATE_PRESSED);
    bool sent = wl_display_flush(display) >= 0;
    const unsigned int steps = 8;
    for (unsigned int i = 1; i <= steps && sent && !cancelled; ++i) {
        struct timespec pause = {.tv_nsec = 15000000};
        while (nanosleep(&pause, &pause) < 0 && errno == EINTR && !cancelled) {}
        if (cancelled)
            break;
        const uint32_t x = ((uint64_t)x1 * (steps - i) + (uint64_t)x2 * i) / steps;
        const uint32_t y = ((uint64_t)y1 * (steps - i) + (uint64_t)y2 * i) / steps;
        move(pointer, x, y, width, height);
        sent = wl_display_flush(display) >= 0;
    }
    button(pointer, WL_POINTER_BUTTON_STATE_RELEASED);
    if (!sync_display(display))
        fputs("Wayland did not confirm drag release.\n", stderr);
    else if (sent && !cancelled)
        result = 0;

cleanup:
    if (pointer)
        zwlr_virtual_pointer_v1_destroy(pointer);
    if (manager)
        zwlr_virtual_pointer_manager_v1_destroy(manager);
    if (registry)
        wl_registry_destroy(registry);
    wl_display_flush(display);
    wl_display_disconnect(display);
    if (cancelled) {
        fputs("Drag cancelled.\n", stderr);
        return 130;
    }
    return result;
}
