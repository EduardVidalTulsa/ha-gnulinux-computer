# GNU+Linux Computer for Home Assistant

<img src="custom_components/gnulinux_computer/brand/icon.png" alt="GNU+Linux Computer" width="96" align="right">

Manage your GNU/Linux computers from Home Assistant over SSH: pending package
updates (and installing them), container image updates, services, volumes,
screenshots, power control, your own commands, GPS position and eSIM profiles.
Nothing has to be installed on the computer besides an SSH server and a user
that can become root with `sudo` or `doas`.

Works on **Arch**, **Debian/Ubuntu** (.deb), **Fedora/RHEL/CentOS/openSUSE**
(.rpm, including image-based systems) and **Alpine**; with systemd, OpenRC or
SysV init.

*[Català a sota](#català)*

## Features

| | |
|---|---|
| **System updates** | One `update` entity per computer, so **Settings** lists each computer once. Its release notes show the install plan — which sources, in which order — and every pending package and container. *Install* runs everything as one detached job, **one source after another, never at the same time**: system packages first, then AUR, Flatpak, Snap and containers. If the system package manager fails, it stops there and nothing else is installed. Progress (step *n* of *m*) is shown while it runs. Firmware is not included. |
| **Package updates** | One `update` entity per source (disabled by default; enable them if you prefer to install source by source): **pacman**, **AUR** (yay/paru), **DNF** (dnf5, dnf, yum), **rpm-ostree / bootc** (Fedora Atomic, CoreOS, RHEL image mode), **zypper**, **APT**, **apk** (Alpine), **Flatpak**, **Snap** and **firmware** (fwupd) — the same sources Plasma Discover and GNOME Software show. Release notes list every package with its current and new version. *Install* runs the upgrade. |
| **Update all** | A button that does the same as installing the *System updates* entity (handy in automations). Failures are reported in a notification. |
| **Update output** | Updates run detached on the computer (they survive a restart of Home Assistant). The **Last update** sensor shows *running / successful / failed*, what ran, when, the exit code and the last lines of output (refreshed live while running). The `gnulinux_computer.update_log` action returns the full output. |
| **Notifications** | A persistent notification when new updates appear, and the `gnulinux_computer_updates_available` event to build your own (mobile) notifications. |
| **Containers** | Only when a Docker or Podman daemon is running: one `update` entity per container (disabled by default, they are part of *System updates*), comparing the local image digest with the registry (HEAD requests only, nothing is pulled). Containers managed by **docker compose** or **Podman quadlets** can be updated from Home Assistant. |
| **Reboot required** | On when the running kernel or NVIDIA driver no longer matches the installed one, or when the distribution asks for it (`/run/reboot-required`, `needs-restarting`, …). It also raises an issue in **Settings → Repairs** saying why; fixing it reboots the computer (refused while updates are being installed). |
| **Services** | Failed services; running / enabled / disabled service lists; one binary sensor per service you choose to monitor; `gnulinux_computer.service_control` action (start, stop, restart, reload, enable, disable). systemd, OpenRC (`rc-service`) and SysV (`service`). |
| **Custom commands** | Define your own commands per computer in the options (`Name = shell command`, one per line). Each one becomes a **button**; the `gnulinux_computer.run_command` action runs one by name and returns exit code and output, and the `gnulinux_computer_command_finished` event fires when it ends. Only commands written in the options can run: there is no action that accepts free text. |
| **Location** | Computers with a GNSS receiver (gpsd or ModemManager) get a `device_tracker` with their position (tested with gpsd; the ModemManager source is untested). Without a receiver (or without a fix), an option (off by default) uses the public IP. A computer with **the same public IP as Home Assistant** shares its Internet connection, so it is placed at home (`fix_source: home_network`). Any other is placed **approximately from its public IP**: Home Assistant asks `ipwho.is` once per address change. That is city level at best and can be far off (mobile operators, VPNs), so it is reported with a 25 km accuracy radius and `fix_source: ip`, together with city, country and whether it is in the EU. The tracker's state is then the city instead of a zone (never `home` by guess). A real GNSS fix always takes precedence and the last one is kept across restarts. |
| **eSIM** | With [lpac](https://github.com/estkme-group/lpac) installed, a select shows the eSIM profiles (factory test profiles are hidden) and switches the active one. Listing is tested on a Quectel EM160R-GL over MBIM; *switching between two profiles is not tested yet.* |
| **Mobile data** | Computers with a modem managed by NetworkManager get a switch that turns the mobile broadband radio on and off. Do not turn it off on a computer you only reach through that modem. |
| **Volumes** | One sensor per mounted filesystem with used percentage, size and free space. Bind mounts and btrfs subvolumes of the same filesystem are grouped. |
| **Screenshot** | Button + `image` entity with the active graphical session: Plasma (spectacle), GNOME, wlroots (grim) and X11 (import/scrot). |
| **Power** | A select with *On* (Wake-on-LAN), *Suspend (RAM)*, *Hibernate (disk)*, *Off* and *Reboot*. The last state is remembered while the computer is off. |
| **System** | Online, load, memory, boot time, init system state and, on laptops, battery level sensors. |
| **Network** | **Connection** sensor: Ethernet, mobile data, VPN, or `Wi-Fi (network name)`, by the interface of the default route (type, interface and SSID as attributes). **IP address** sensor (address of the default route), one **IP &lt;interface&gt;** sensor per network interface (LAN, Wi-Fi, mobile data, Tailscale, WireGuard…) and **Public IP** sensor (IPv4 and IPv6 as seen from the Internet; the computer asks `api.ipify.org` every 5 minutes — can be turned off in the options). |

## How it compares

Most other ways to bring a Linux computer into Home Assistant are **companion
agents** (Go Hass Agent, System Bridge, Deskmate, halinuxcompanion, KDE
Connect based ones…): a program installed on the computer, usually running in
the user's desktop session. They are the right tool for *the PC in front of
you*. This integration is meant for **managing machines**, headless servers
included:

- **Agentless**: nothing to install or keep running on the computer, only an
  SSH server and `sudo`/`doas`. It works the same on a desktop, a Raspberry Pi
  or a server without a graphical session.
- **Updates you can install**, not just a sensor: every package manager listed
  above plus container images, as native `update` entities, run as detached
  jobs in a safe order.
- **Administration**: reboot required (with a repair that reboots), services,
  custom commands, Wake-on-LAN across networks, several distributions and init
  systems.

What it does **not** do: notifications on the computer, active window, idle or
lock state, camera/microphone, media, brightness or volume. It polls (every 30
seconds by default) instead of pushing. And it needs SSH access with root
rights, which is more trust than an unprivileged agent: see
[Security](#security). The two kinds work well together, e.g. this
integration to keep machines updated and a companion agent on your desktop.

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
(e.g. member of `wheel` or `sudo`) or passwordless `doas`
(`permit nopass <user>` in `doas.conf`, usual on Alpine). Three ways to
authenticate:

- **Username and password** – the password is also used for `sudo`.
  The SSH server must allow password authentication.
- **Existing SSH key** – path of a private key inside Home Assistant,
  e.g. `/config/.ssh/id_ed25519`.
- **Shared SSH key of Home Assistant** – one ed25519 key pair,
  `/config/.ssh/gnulinux_computer`, is created the first time and used for
  **every** computer; its public key is shown, ready to append to
  `~/.ssh/authorized_keys`. See [One key for the whole pool](#one-key-for-the-whole-pool).

If `sudo` asks for a password, give it in the *sudo password* field: it is sent
over the SSH channel to an `askpass` helper and never written to disk.
The server host key is pinned on first connection.

If the computer later rejects the credentials (password changed, key removed)
or its host key changes, Home Assistant **stops connecting** — no failed login
every 30 seconds, no fail2ban lock-out — and asks you to re-authenticate.

### One key for the whole pool

All computers share the same Home Assistant key, so if Home Assistant is ever
compromised there is exactly one credential to revoke. On every computer:

```sh
sed -i '/home-assistant@gnulinux_computer/d' ~/.ssh/authorized_keys
```

Computers added with a password or with an older per-computer key can be moved
to the shared key with the `gnulinux_computer.use_shared_key` action
(Developer tools → Actions): it authorizes the shared key using the current
credentials, checks that it works, switches, and removes the old per-computer
key. Leave the computer empty to do it for all of them.

A good hardening step is to restrict the key in `authorized_keys` to the
address of Home Assistant: `from="192.0.2.10" ssh-ed25519 AAAA…`.

### Custom commands

In the options, one command per line:

```
Backup = $SUDO systemctl start backup.service
Free space = df -h /
```

`$SUDO` runs as root (sudo or doas, whatever the computer uses). Commands run
under POSIX `sh`, with a 5 minute timeout.

```yaml
action: gnulinux_computer.run_command
data:
  config_entry_id: "…"
  command: Backup
response_variable: result   # exit_code, stdout, stderr, duration
```

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
- **Through a MikroTik router** over SSH: it logs in over
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
| Alpine | `sudo`, or `doas` with `permit nopass`; OpenRC services are detected automatically |
| Location | `gpsd` (`gpspipe`), or ModemManager with GPS enabled (`mmcli --location-enable-gps-raw`) |
| eSIM | `lpac` built with the driver your modem needs (MBIM/QMI builds are not always packaged); its `LPAC_*` settings go in `/etc/default/lpac`. **Set the UIM slot of the eSIM** (e.g. `LPAC_APDU_MBIM_UIM_SLOT=2`): with the default slot lpac switches the modem to slot 1 and the connection drops. |
| AUR | `yay` or `paru`; does not work when logging in as `root` |
| Screenshots | `spectacle`, `gnome-screenshot`, `grim`, `import` or `scrot` |
| Suspend / hibernate | working sleep states (hibernate needs swap + `resume`) |
| Wake-on-LAN | WoL enabled in the firmware and network card |

## Security

The SSH user is root-equivalent on the computer: Home Assistant can upgrade
packages, control services and power, and run your custom commands. Treat
access to Home Assistant accordingly.

- All actions of this integration are **administrator-only**. Buttons,
  selects and update entities follow the normal Home Assistant permissions.
- Custom commands are fixed by an administrator in the options; nothing
  accepts free-text commands.
- The host key is pinned (and a change is never accepted silently).
  Rejected credentials are not retried.
- The sudo password travels over the SSH channel to an `askpass` helper and
  is never written to disk on the computer. It is stored, like every Home
  Assistant credential, in clear text in `.storage/core.config_entries`:
  prefer the shared key plus passwordless sudo for a dedicated user.
- Service names, MAC addresses, interface names and ICCIDs are validated
  before they reach a shell or a router.
- Registry token URLs are only followed to public https hosts.

## Languages

English, Català, Castellano, Français, Português, Nederlands, 中文, हिन्दी,
العربية, বাংলা and עברית.

## License

Copyright © 2026 Eduard Vidal Tulsà.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either **version 3 of the License, or (at your option) any later
version**. See [LICENSE](LICENSE).

---

## Català

Integració per controlar ordinadors GNU/Linux des de Home Assistant per SSH:

- **Sortida de l'última actualització** (sensor *Darrera actualització* i acció `update_log`); les actualitzacions corren en segon pla a la màquina.
- Funciona a **Arch**, **Debian/Ubuntu** (.deb), **Fedora/RHEL/CentOS/openSUSE**
  (.rpm, també els sistemes d'imatge amb rpm-ostree/bootc) i **Alpine**.
- **Ordres personalitzades** per ordinador: cada una és un botó i es pot
  executar amb l'acció `run_command`.
- **Una sola clau SSH** per a tots els ordinadors (`/config/.ssh/gnulinux_computer`):
  si mai queda compromesa només cal esborrar una línia a cada màquina
  (`sed -i '/home-assistant@gnulinux_computer/d' ~/.ssh/authorized_keys`).
  L'acció `use_shared_key` hi migra els ordinadors que ja tens.
- **Reautenticació**: si l'ordinador rebutja les credencials, Home Assistant
  deixa d'intentar-ho i et demana les noves.
- **Connexió** de cada màquina: Ethernet, dades mòbils, VPN o `Wi-Fi (nom de la xarxa)`.
- **Adreces IP** de la màquina i **IP pública** (IPv4 i IPv6) que detecta.
- **Ubicació aproximada per la IP pública** (opcional) quan no hi ha GPS.
- **Ubicació** (GPS amb gpsd, provat; o ModemManager, sense provar) i **perfil eSIM**
  (lpac, encara sense provar amb maquinari real).
- **Actualitzacions del sistema**: una sola entitat per ordinador (a **Configuració**
  surt un cop per màquina) que ho instal·la tot en una sola tasca, **una font darrere
  l'altra i mai a la vegada**: paquets del sistema, AUR, Flatpak, Snap i contenidors.
  Si fallen els paquets del sistema, s'atura. Les notes mostren l'ordre i tots els paquets.
- **Actualitzacions per font** de pacman, AUR, DNF/yum, rpm-ostree, zypper, APT, apk, Flatpak,
  Snap i microprogramari (desactivades per defecte, excepte el microprogramari), i botó
  **Actualitza-ho tot**.
- **Notificació** quan apareixen actualitzacions noves.
- **Contenidors** Docker/Podman (només si el dimoni està en marxa): avisa quan hi
  ha imatge nova i actualitza els de compose/quadlet.
- **Cal reiniciar** quan el nucli o el controlador NVIDIA en marxa no és
  l'instal·lat; també surt a **Configuració → Reparacions**, i reparar-ho reinicia
  l'ordinador (no mentre s'instal·len actualitzacions).
- **Serveis**: fallats, en marxa, activats i desactivats; sensors dels serveis
  que triïs i acció per iniciar-los, aturar-los, etc.
- **Volums**: percentatge ocupat de cada sistema de fitxers.
- **Captura de pantalla** de la sessió gràfica.
- **Energia**: encès (Wake-on-LAN), suspès (RAM), hibernat (disc), apagat i
  reinici. Si l'ordinador és en una altra xarxa, el paquet màgic el pot enviar
  un altre ordinador de la integració o un router MikroTik d'aquella xarxa (per SSH).

**Comparat amb altres opcions**: la majoria (Go Hass Agent, System Bridge,
Deskmate, halinuxcompanion, KDE Connect…) són **agents** que s'instal·len a
l'ordinador i solen córrer a la sessió d'escriptori; són ideals per al PC que
tens davant. Aquesta integració és per **administrar màquines**, servidors
inclosos: no s'hi instal·la res (només SSH i `sudo`/`doas`), instal·la les
actualitzacions de tots els gestors de paquets i dels contenidors, i gestiona
reinicis, serveis, ordres i Wake-on-LAN. No fa notificacions a l'ordinador, ni
estat d'inactivitat/bloqueig, càmera, multimèdia, brillantor o volum; consulta
cada 30 s en lloc de rebre avisos, i necessita accés SSH amb permisos de root.
Les dues coses es complementen.

Instal·lació: Marketplace/HACS → *Repositoris personalitzats* →
`https://github.com/EduardVidalTulsa/ha-gnulinux-computer` (Integració).
Després: *Configuració → Dispositius i serveis → Afegeix integració →
GNU+Linux Computer*. La interfície està traduïda al català.
