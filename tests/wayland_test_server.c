#include <stdio.h>
#include <wayland-server.h>

#include "virtual-pointer-server.h"

static void destroy(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    wl_resource_destroy(resource);
}

static void absolute(struct wl_client *client, struct wl_resource *resource,
                     uint32_t time, uint32_t x, uint32_t y, uint32_t width, uint32_t height) {
    (void)client;
    (void)resource;
    (void)time;
    printf("move %u %u %u %u\n", x, y, width, height);
}

static void button(struct wl_client *client, struct wl_resource *resource,
                   uint32_t time, uint32_t code, uint32_t state) {
    (void)client;
    (void)resource;
    (void)time;
    printf("%s %u\n", state ? "press" : "release", code);
}

static void frame(struct wl_client *client, struct wl_resource *resource) {
    (void)client;
    (void)resource;
    puts("frame");
}

static const struct zwlr_virtual_pointer_v1_interface POINTER = {
    .motion_absolute = absolute,
    .button = button,
    .frame = frame,
    .destroy = destroy,
};

static void create(struct wl_client *client, struct wl_resource *manager,
                   struct wl_resource *seat, uint32_t id) {
    (void)manager;
    (void)seat;
    struct wl_resource *pointer = wl_resource_create(client, &zwlr_virtual_pointer_v1_interface, 1, id);
    if (pointer)
        wl_resource_set_implementation(pointer, &POINTER, NULL, NULL);
    else
        wl_client_post_no_memory(client);
}

static const struct zwlr_virtual_pointer_manager_v1_interface MANAGER = {
    .create_virtual_pointer = create,
    .destroy = destroy,
};

static void bind_manager(struct wl_client *client, void *data, uint32_t version, uint32_t id) {
    (void)data;
    struct wl_resource *manager = wl_resource_create(client, &zwlr_virtual_pointer_manager_v1_interface, version, id);
    if (manager)
        wl_resource_set_implementation(manager, &MANAGER, NULL, NULL);
    else
        wl_client_post_no_memory(client);
}

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    struct wl_display *display = wl_display_create();
    if (!display || !wl_global_create(display, &zwlr_virtual_pointer_manager_v1_interface, 1, NULL, bind_manager))
        return 1;
    const char *socket = wl_display_add_socket_auto(display);
    if (!socket)
        return 2;
    puts(socket);
    wl_display_run(display);
    wl_display_destroy(display);
    return 0;
}
