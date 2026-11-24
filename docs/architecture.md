# Architecture and upstream review

## What fits Arch and Hyprland

| Candidate | Source findings | Decision |
| --- | --- | --- |
| Hyprland IPC and grim | Hyprland exposes monitor, window, focus, and dispatch information. Recent releases dispatch through Lua, so commands must come from fixed templates and validate values. The Arch grim package supports named outputs, regions, and foreign toplevel IDs, plus configurable image scale. Hyprland is BSD-3-Clause and grim is MIT. | Reuse both for desktop state, window operations, and screenshots. Keep image bytes in memory where practical. |
| Hyprhands | Its Rust source uses grim, compositor IPC, virtual devices and AT-SPI. It can fall back to a daemon with `/dev/uinput` access, and its drag specifically uses that fallback. Its capture source explains why the window rectangle can include occluding surfaces. MIT. | Use as a technical reference. Keep pointer operations in the current Wayland seat and avoid elevating input privileges. |
| hyprland-mcp | Its Python backend uses grim, Hyprland IPC, AT-SPI, and ydotool for clicks, scrolling, and drag. GPL-2.0 applies to that fork. | Use as a technical reference. Avoid a daemon and copying source into this repo. |
| computer-use-mcp | Its Rust entry point starts an MCP server for a nested desktop. It supports an HTTP transport with a token and uses a separate session for apps. | Compare the isolation model. Keep the initial integration on local stdio and qualify interactions in a disposable session or app. |
| XDG Desktop Portal | Remote Desktop can authorize a session for input on supported backends. The current Hyprland backend advertises Screenshot, ScreenCast, GlobalShortcuts and InputCapture. InputCapture does not supply the Remote Desktop injection methods. | Keep portal Remote Desktop as a future alternative where the installed backend supports it. |
| AT-SPI | Client libraries and the accessibility D-Bus registry expose toolkit-provided roles, names, and states. Supported UI and coordinates vary by toolkit and app. | Offer bounded, read-only discovery when available. Treat data as untrusted and keep text values and secure fields out of the output. |

Sources include the [Hyprland IPC reference](https://wiki.hypr.land/configuring/core/advanced-configuration/using-hyprctl/), [Arch grim manual](https://man.archlinux.org/man/grim.1.en), [Hyprhands input](https://github.com/berker-z/hyprhands/blob/main/src/input.rs), [Hyprhands capture](https://github.com/berker-z/hyprhands/blob/main/src/capture.rs), [hyprland-mcp input](https://github.com/LVladymyr/hyprland-mcp/blob/main/hyprland_mcp/input.py), [computer-use-mcp entry point](https://github.com/ALLLLLL703/computer-use-mcp/blob/main/src/main.rs), [Hyprland portal manifest](https://github.com/hyprwm/xdg-desktop-portal-hyprland/blob/main/hyprland.portal), [Remote Desktop API](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html), and [AT-SPI developer reference](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/).

## Capture and input contracts

Use logical coordinates for input. Keep the signed logical origin and captured dimensions alongside the image, and translate an image point relative to that region. At capture scale 1, logical and image pixels can be mapped directly when capture bounds and dimensions agree. Validate coordinates against occupied monitor rectangles, since the desktop's bounding box can include gaps. Capture a window by its stable foreign-toplevel identifier. Fail if isolated capture is unavailable, since cropping a screen rectangle can expose content from overlapping windows. Use a fresh screenshot and window identity after a focus, resize, navigation, or recovery step.

A compositor-accepted event shows delivery to the compositor. Verify the application effect separately using a fresh screenshot or read-only state. Stop when the target disappears, changes unexpectedly, or no longer matches the user's task.

A drag requires a single virtual device from button press through release. The native client binds the Wayland virtual-pointer protocol, emits bounded steps, and queues release if it receives a stop signal. It confirms delivery with a bounded display sync. The same client handles pointer movement using the complete logical output bounds. In an isolated two-output test, wdotool's primary-output extent produced an incorrect coordinate and Hyprland's cursor dispatcher moved the cursor without notifying the GTK app. The native client also avoids root and `/dev/uinput`. The pinned wdotool backend still supplies buttons, scrolling, and keyboard events. Vendored protocol matches [upstream wlr-protocols revision b010a036](https://github.com/swaywm/wlr-protocols/blob/b010a03648b88d143236de193bddbfea0c08bc84/unstable/wlr-virtual-pointer-unstable-v1.xml), with its embedded license.

Build the native helper and run its standalone tests from the repository directory.

```sh
mkdir -p native/build
native/build.sh native/build/drag
python -m unittest discover -s tests -p test_native_drag.py -v
```

The tests compile a minimal Wayland server in a private temporary runtime directory and connect the helper only to that socket. The server checks button release after both normal use and a stop signal. Invalid coordinates and unavailable display use the same private runtime directory without a compositor.

## Integration and trust boundaries

Use Codex's stdio MCP transport so only the process Codex starts receives its tool requests. The runtime approval and stop mechanism control display access separately. Approval should identify the requested scope and come from the user outside agent-generated tool arguments. Use one owner for each control session. Keep raw screenshot pixels, clipboard contents, typed text, window titles and accessibility names out of audit logs. Screen content and accessibility names can contain prompt injection. Neither may authorize more tool access or external actions.

A test compositor needs its own Wayland socket and a dedicated configuration with no user autostarts. Verify its instance and socket before passing either to capture or input tools. On Hyprland, a nested session can request an additional Wayland output via Hyprland IPC. Test an isolated layout with offset and scaled monitors before trusting cross-monitor transforms. Use a disposable app with known UI and verify received events. Keep clipboard and live desktop actions behind their own permissions. [Hyprland nested output documentation](https://wiki.hypr.land/configuring/core/advanced-configuration/using-hyprctl/).
