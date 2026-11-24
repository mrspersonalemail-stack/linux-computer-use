# Verification and handoff

Tested on Arch Linux with Hyprland 0.57.2. The normal desktop received no synthetic input. The integration test starts its own D-Bus session, headless Sway, nested Hyprland and disposable GTK applications. Bubblewrap hides the live runtime sockets, disables networking, and exposes GPU render nodes without exposing the display controller devices.

## Results

| Area | Evidence |
| --- | --- |
| Unit and protocol tests | Coverage includes redacted audit records, single action ownership, expired leases, cancellation, focus changes, coordinate bounds, bounded capture output, prompt injection through app labels, clipboard consent, MCP discovery, and quota handling. |
| Desktop integration | Captured window, monitor and desktop images. Verified PNG sizes and selected GTK pixels. Tested a negative-origin 1.25× output and an independent 1× output. Checked pointer coordinates within 2 logical units and verified GTK motion, click, scroll, drag, key, and text events. |
| Desktop controls | Verified focus and resize, real clipboard round-trip, AT-SPI lookup, discovery and relaunch through an installed fixture desktop entry. A separate operator test opened the real grant dialog, observed the indicator and revoked the lease through the stop command. Individual action approvals were simulated in the isolated application test. |
| Live release check | An operator approved a control grant and individual actions on a disposable GTK window. Live screenshots, mouse click, and keyboard input succeeded. A window-only screenshot confirmed the fixture received both types of input. The stop command revoked access and a follow-up capture was rejected. |
| Emergency stop | Fixture command cancellation prevented a delayed side effect. An isolated Wayland protocol server observed release of the pressed drag button after SIGTERM. |
| Codex integration | Codex App Server discovered all five tools through the global stdio registration. The transport test exercised structured status and rejected capture without a session. |
| Usage | The supported account API returned both subscription windows and reset times. Tests checked staleness, threshold boundaries and thread-bound context. Use the live chat's own status for current context when a matching notification is unavailable. |

## Measurements

The MCP process started in 275 ms and used 63.2 MiB resident memory. Thirty paused-status calls succeeded, with a 13.9 ms median and 15.4 ms p95. The process used 0.09 CPU seconds across the calls and no measurable CPU time during a two-second idle sample.

Across four isolated runs, window screenshot calls took 18–31 ms, monitor captures 32–222 ms, and full-screen captures 37–230 ms. These are fixture measurements on one host. Larger and more complex desktop layouts can take longer.

## Operational limits

Same-user app permissions or other already-authorized Wayland clients are outside this bridge's control. Screenshot content and application labels are untrusted. Accessibility depends on application support.
