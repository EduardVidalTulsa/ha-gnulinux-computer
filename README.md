# GNU+Linux Computer for Home Assistant

<img src="custom_components/gnulinux_computer/brand/icon.png" alt="GNU+Linux Computer" width="96" align="right">

Manage your GNU/Linux computers from Home Assistant over SSH: pending package
updates (and installing them), container image updates, systemd services,
volumes, screenshots and power control. Nothing has to be installed on the
computer besides an SSH server and a user that can use `sudo`.

*[Català a sota](#català)*

## Features

| | |
|---|---|
| **Package updates** | One `update` entity per source: **pacman**, **AUR** (yay/paru), **DNF**, **zypper**, **APT**, **Flatpak**, **Snap** and **firmware** (fwupd) — the same sources Plasma Discover and GNOME Software show. Release notes list every package with its current and new version. *Install* runs the upgrade. |
| **Update all** | A button that upgrades everything in a safe order: system packages, AUR, Flatpak, Snap, then containers. Failures are reported in a notification. |
| **Notifications** | A persistent notification when new updates appear, and the `gnulinux_computer_updates_available` event to build your own (mobile) notifications. |
| **Containers** | Only when a Docker or Podman daemon is running: one `update` entity per container, comparing the local image digest with the registry (HEAD requests only, nothing is pulled). Containers managed by **docker compose** or **Podman quadlets** can be updated from Home Assistant. |
| **Reboot required** | On when the running kernel or NVIDIA driver no longer matches the installed one, or when the distribution asks for it (`/run/reboot-required`, `needs-restarting`, …). |
| **Services** | Failed services; running / enabled / disabled service lists; one binary sensor per service you choose to monitor; `gnulinux_computer.service_control` action (start, stop, restart, reload, enable, disable). Non-systemd systems fall back to `service`. |
| **Volumes** | One sensor per mounted filesystem with used percentage, size and free space. Bind mounts and btrfs subvolumes of the same filesystem are grouped. |
| **Screenshot** | Button + `image` entity with the active graphical session: Plasma (spectacle), GNOME, wlroots (grim) and X11 (import/scrot). |
| **Power** | A select with *On* (Wake-on-LAN), *Suspend (RAM)*, *Hibernate (disk)*, *Off* and *Reboot*. The last state is remembered while the computer is off. |
| **System** | Online, load, memory, boot time and systemd state sensors. |

## Installation

### Marketplace / HACS

1. Open **Marketplace** (or HACS) → ⋮ → **Custom repositories**.
2. Add `https://github.com/EduardVidalTulsa/ha-gnulinux-computer` as an **Integration**.
3. Install **GNU+Linux Computer** and restart Home Assistant.

### Manual

Copy `custom_components/gnulinux_computer` into the `custom_components` folder
of your configuration and restart Home Assistant.

## Configuration

**Settings → Devices & services → Add integration → GNU+Linux Computer**

On the computer you need an SSH server and a user that can run `sudo`
(e.g. member of `wheel` or `sudo`). Three ways to authenticate:

- **Username and password** – the password is also used for `sudo`.
  The SSH server must allow password authentication.
- **Existing SSH key** – path of a private key inside Home Assistant,
  e.g. `/config/.ssh/id_ed25519`.
- **Generate a new SSH key** – an ed25519 key is created in `/config/.ssh/` and
  the public key is shown, ready to append to `~/.ssh/authorized_keys`.

If `sudo` asks for a password, give it in the *sudo password* field: it is sent
over the SSH channel to an `askpass` helper and never written to disk.
The server host key is pinned on first connection.

Options (⚙ on the integration): services to monitor, notifications, update
check interval (default 60 min), status interval (default 30 s) and the
Wake-on-LAN MAC / broadcast address (detected automatically).

### Wake-on-LAN across networks

Magic packets are broadcasts and do not cross routers. When the computer is in
another network than Home Assistant (VPN, another site…), choose in the options
how it is woken up:

- **Broadcast from Home Assistant** – default, same network only.
- **Through another computer** of this integration that is always on in the
  same network (it needs `wakeonlan` or `python3`).
- **Through a MikroTik router added to Home Assistant** with the core
  [MikroTik integration](https://www.home-assistant.io/integrations/mikrotik):
  its existing API connection sends `/tool/wol`, no extra user or key needed.
  The API user of the router needs the **test** policy.
- **Through a MikroTik router** over SSH (routers not in Home Assistant): it logs in over
  SSH and runs `/tool wol`. A key is generated in `/config/.ssh/gnulinux_mikrotik`
  and the options form shows the RouterOS commands to create a restricted user:

  ```
  /user group add name=wol policy=ssh,read,test,!local,!telnet,!ftp,!reboot,!write,!policy,!winbox,!password,!web,!sniff,!sensitive,!api,!romon,!rest-api
  /user add name=homeassistant group=wol password=<long random password>
  /user ssh-keys add user=homeassistant key="ssh-ed25519 AAAA… home-assistant@gnulinux_computer"
  ```

### Notification to your phone

```yaml
automation:
  - alias: Linux updates available
    triggers:
      - trigger: event
        event_type: gnulinux_computer_updates_available
    actions:
      - action: notify.mobile_app_my_phone
        data:
          title: "Updates on {{ trigger.event.data.computer }}"
          message: "{{ trigger.event.data.total }} pending ({{ trigger.event.data.new }} new)"
```

## Requirements on the computer

| Feature | Needs |
|---|---|
| pacman updates | `pacman-contrib` (`checkupdates`) |
| AUR | `yay` or `paru`; does not work when logging in as `root` |
| Screenshots | `spectacle`, `gnome-screenshot`, `grim`, `import` or `scrot` |
| Suspend / hibernate | working sleep states (hibernate needs swap + `resume`) |
| Wake-on-LAN | WoL enabled in the firmware and network card |

## Security

The SSH user has full `sudo` rights and Home Assistant can run package
upgrades and power actions on the computer. Use a dedicated key, keep your
Home Assistant instance secure, and consider a dedicated system user.

## License

Copyright © 2026 Eduard Vidal Tulsà.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either **version 3 of the License, or (at your option) any later
version**. See [LICENSE](LICENSE).

---

## Català

Integració per controlar ordinadors GNU/Linux des de Home Assistant per SSH:

- **Actualitzacions** de pacman, AUR, DNF, zypper, APT, Flatpak, Snap i
  microprogramari, amb botó d'instal·lar i botó **Actualitza-ho tot**.
- **Notificació** quan apareixen actualitzacions noves.
- **Contenidors** Docker/Podman (només si el dimoni està en marxa): avisa quan hi
  ha imatge nova i actualitza els de compose/quadlet.
- **Cal reiniciar** quan el nucli o el controlador NVIDIA en marxa no és
  l'instal·lat.
- **Serveis**: fallats, en marxa, activats i desactivats; sensors dels serveis
  que triïs i acció per iniciar-los, aturar-los, etc.
- **Volums**: percentatge ocupat de cada sistema de fitxers.
- **Captura de pantalla** de la sessió gràfica.
- **Energia**: encès (Wake-on-LAN), suspès (RAM), hibernat (disc), apagat i
  reinici. Si l'ordinador és en una altra xarxa, el paquet màgic el pot enviar
  un altre ordinador de la integració o un router MikroTik d'aquella xarxa
  (el mateix que ja tinguis a Home Assistant amb la integració MikroTik, o per SSH).

Instal·lació: Marketplace/HACS → *Repositoris personalitzats* →
`https://github.com/EduardVidalTulsa/ha-gnulinux-computer` (Integració).
Després: *Configuració → Dispositius i serveis → Afegeix integració →
GNU+Linux Computer*. La interfície està traduïda al català.
