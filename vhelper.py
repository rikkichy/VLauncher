#!/usr/bin/env python3

import gi
import json
import os
import shlex
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

STEAM_ROOT = Path.home() / ".local/share/Steam"
VTS_APPID = "1325860"
VTS_COMPAT = STEAM_ROOT / "steamapps/compatdata" / VTS_APPID
VTS_PREFIX = VTS_COMPAT / "pfx"
SHOOST_DIR = VTS_PREFIX / "drive_c/Shoost"
SPOUT2PW_DIR = Path.home() / ".local/share/spout2pw"
OBS_PWVIDEO_USER_DIR = Path.home() / ".config/obs-studio/plugins/obs-pwvideo"
CONFIG_FILE = Path.home() / ".config/vhelper.json"


def detect_proton_from_config():
    config = VTS_COMPAT / "config_info"
    if not config.exists():
        return None
    lines = config.read_text().splitlines()
    for line in lines:
        if "compatibilitytools.d" in line and "files/" in line:
            base = line.split("/files/")[0]
            return Path(base)
    return None


def find_shoost_exe():
    if not SHOOST_DIR.exists():
        return None
    for exe in sorted(SHOOST_DIR.glob("*.exe")):
        name = exe.name.lower()
        if "shoost" in name and "crash" not in name and "unity" not in name.replace("shoost", ""):
            return exe
    return None


def find_spout2pw():
    """Return the runtime spout2pw.sh path. Steam pressure-vessel only mounts
    $HOME and a handful of other paths into the Proton sandbox, so this must
    live under the user's home, not /usr/local/share."""
    p = SPOUT2PW_DIR / "spout2pw.sh"
    return p if p.exists() else None


def extract_bundle_to_home():
    """Copy the vendored bundle into SPOUT2PW_DIR. Returns True on success."""
    bundle = find_bundled_spout2pw()
    if not bundle:
        return False
    if SPOUT2PW_DIR.exists():
        shutil.rmtree(SPOUT2PW_DIR)
    shutil.copytree(bundle, SPOUT2PW_DIR)
    (SPOUT2PW_DIR / "spout2pw.sh").chmod(0o755)
    return True


def detect_vhelper_install():
    """Return install prefix Path if vhelper was launched from an install
    (e.g. /usr/local), or None when running from a source checkout."""
    py = Path(__file__).resolve()
    if py.parent.name != "vhelper" or py.parent.parent.name != "share":
        return None
    prefix = py.parent.parent.parent
    if not (prefix / "bin/vhelper").exists():
        return None
    return prefix


def find_bundled_spout2pw():
    """Return the directory containing the vendored spout2pw files, or None."""
    py = Path(__file__).resolve()
    for cand in (py.parent / "spout2pw-bundle", py.parent / "data/spout2pw"):
        if (cand / "spout2pw.sh").exists():
            return cand
    return None


def find_bundled_obs_pwvideo():
    """Return the directory containing the vendored obs-pwvideo plugin, or None.

    Layout matches upstream's prebuilt tarball:
        bin/64bit/obs-pwvideo.so
        data/locale/en-US.ini
    """
    py = Path(__file__).resolve()
    for cand in (py.parent / "obs-pwvideo-bundle", py.parent / "data/obs-pwvideo"):
        if (cand / "bin/64bit/obs-pwvideo.so").exists():
            return cand
    return None


def is_obs_pwvideo_installed():
    return (OBS_PWVIDEO_USER_DIR / "bin/64bit/obs-pwvideo.so").exists()


def check_obs_pwvideo_deps(so_path):
    """Return a list of unresolved SONAMEs reported by ldd, or [] on success.

    The upstream prebuilt .so links libobs.so.30 — if the user's OBS bumped
    its major (e.g. OBS 31 ships libobs.so.31), the plugin won't load. We
    warn before copying so the user isn't left wondering why OBS ignores it."""
    if not shutil.which("ldd"):
        return []
    try:
        result = subprocess.run(
            ["ldd", str(so_path)],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    missing = []
    for line in result.stdout.splitlines():
        if "not found" in line.lower():
            soname = line.strip().split()[0]
            missing.append(soname)
    return missing


def load_config():
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def _process_environment_value(proc_dir, name):
    """Read one environment value from a process, tolerating process exits."""
    try:
        entries = (proc_dir / "environ").read_bytes().split(b"\0")
    except (OSError, PermissionError):
        return None
    prefix = os.fsencode(name) + b"="
    for entry in entries:
        if entry.startswith(prefix):
            return os.fsdecode(entry[len(prefix):])
    return None


def find_vts_process():
    """Return a VTube Studio process using its expected Wine prefix."""
    try:
        processes = Path("/proc").iterdir()
    except OSError:
        return None
    for proc_dir in processes:
        if not proc_dir.name.isdigit():
            continue
        try:
            argv = (proc_dir / "cmdline").read_bytes().split(b"\0")
        except (OSError, PermissionError):
            continue
        if not argv or not argv[0]:
            continue
        executable = os.fsdecode(argv[0]).replace("\\", "/").rsplit("/", 1)[-1]
        if executable.casefold() != "vtube studio.exe":
            continue
        prefix = _process_environment_value(proc_dir, "WINEPREFIX")
        if not prefix:
            continue
        try:
            if Path(prefix).resolve() == VTS_PREFIX.resolve():
                return int(proc_dir.name)
        except OSError:
            continue
    return None


def _vts_uses_private_tmp(vts_pid):
    """Return whether VTube Studio has a container-private /tmp."""
    host_tmp = os.stat("/tmp")
    vts_tmp = os.stat(f"/proc/{vts_pid}/root/tmp")
    return (
        host_tmp.st_dev != vts_tmp.st_dev
        or host_tmp.st_ino != vts_tmp.st_ino
    )


def _vts_proton_launcher(vts_pid):
    """Return the Proton launcher used by the running VTube Studio process."""
    proc_dir = Path(f"/proc/{vts_pid}")
    tool_paths = _process_environment_value(
        proc_dir, "STEAM_COMPAT_TOOL_PATHS"
    )
    if tool_paths:
        for tool_path in tool_paths.split(os.pathsep):
            candidate = Path(tool_path) / "proton"
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate

    configured = detect_proton_from_config()
    if configured:
        candidate = configured / "proton"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _vts_namespace_command(vts_pid, argv):
    """Build a command that executes argv inside VTube Studio's container."""
    nsenter = shutil.which("nsenter")
    if not nsenter:
        return None
    return [
        nsenter,
        "--target", str(vts_pid),
        "--user",
        "--mount",
        "--preserve-credentials",
        "--env",
        "--root",
        "--wd",
        "--",
        "/usr/bin/env",
        "-u", "WINESERVERSOCKET",
        *map(str, argv),
    ]


def build_shoost_launch_command(shoost_exe=None):
    """Return (command, summary, blocker) for launching Shoost right now."""
    vts_pid = find_vts_process()
    if vts_pid is None:
        blocker = (
            "VTube Studio is not running. Start it from Steam before launching "
            "Shoost so both applications can share one Wine runtime."
        )
        return None, "Not running", blocker

    try:
        private_runtime = _vts_uses_private_tmp(vts_pid)
    except OSError:
        blocker = (
            f"VTube Studio is running as PID {vts_pid}, but VHelper cannot "
            "inspect its Steam Runtime. Shoost was not launched because "
            "Spout2 requires both applications to share one Wine server."
        )
        return None, f"Runtime inspection failed (PID {vts_pid})", blocker

    if private_runtime:
        nsenter = shutil.which("nsenter")
        if not nsenter:
            blocker = (
                "VTube Studio is running in a private Steam container, but "
                "nsenter is not available. Install util-linux so VHelper can "
                "launch Shoost in VTube Studio's runtime."
            )
            return None, f"nsenter not found (PID {vts_pid})", blocker

        proton = _vts_proton_launcher(vts_pid)
        if not proton:
            blocker = (
                f"VTube Studio is running as PID {vts_pid}, but VHelper cannot "
                "locate the Proton launcher used by that process."
            )
            return None, f"Proton launcher not found (PID {vts_pid})", blocker

        command = _vts_namespace_command(vts_pid, [proton, "run"])
        if shoost_exe is not None:
            command.append(str(shoost_exe))
        return (
            command,
            f"Shared runtime launcher ready (PID {vts_pid})",
            None,
        )

    launcher = shutil.which("protontricks-launch")
    if not launcher:
        blocker = "protontricks-launch is not available."
        return None, "protontricks-launch not found", blocker

    command = [launcher, "--appid", VTS_APPID]
    if shoost_exe is not None:
        command.append(str(shoost_exe))
    return command, f"Compatible runtime detected (PID {vts_pid})", None


def build_shoost_stop_command(shoost_exe):
    """Build a taskkill command for a Shoost process in VTS's container."""
    vts_pid = find_vts_process()
    if vts_pid is None:
        return None
    try:
        if not _vts_uses_private_tmp(vts_pid):
            return None
    except OSError:
        return None

    proton = _vts_proton_launcher(vts_pid)
    if not proton:
        return None
    wine = proton.parent / "files/bin/wine"
    if not wine.is_file():
        return None
    return _vts_namespace_command(
        vts_pid,
        [wine, "taskkill", "/F", "/IM", shoost_exe.name],
    )


def inspect_shoost_launch_runtime():
    """Return (safe, summary, blocker) for launching Shoost right now."""
    command, summary, blocker = build_shoost_launch_command()
    return command is not None, summary, blocker


class VHelperWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="VHelper", default_width=520, default_height=580)

        self.proton_path = detect_proton_from_config()
        self.shoost_proc = None
        self.shoost_exe = find_shoost_exe()
        self.install_prefix = detect_vhelper_install()
        self.bundled_spout2pw = find_bundled_spout2pw()
        self.bundled_obs_pwvideo = find_bundled_obs_pwvideo()
        self.shoost_launch_runtime = inspect_shoost_launch_runtime()

        if not find_spout2pw() and self.bundled_spout2pw:
            try:
                extract_bundle_to_home()
            except Exception:
                pass

        self.spout2pw_sh = find_spout2pw()

        header = Adw.HeaderBar()
        settings_btn = Gtk.Button(icon_name="emblem-system-symbolic")
        settings_btn.set_tooltip_text("Settings")
        settings_btn.connect("clicked", self.on_open_settings)
        header.pack_end(settings_btn)

        self._main_view = self._build_main_view()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.append(header)
        self._body_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, vexpand=True)
        outer.append(self._body_slot)
        self.set_content(outer)

        self._apply_view()
        self._update_buttons()
        self._runtime_check_source = GLib.timeout_add_seconds(
            2, self._poll_shoost_launch_runtime
        )

    def _build_main_view(self):
        """Construct the normal vhelper body. Always built — even when the
        onboarding view is showing — so handlers and self.log_buf/self.*_row
        exist regardless of which view is currently in the body slot."""
        scrollable = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=600)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        inner.set_margin_top(24)
        inner.set_margin_bottom(24)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        clamp.set_child(inner)
        scrollable.set_child(clamp)

        status_group = Adw.PreferencesGroup(title="Status")

        self.vts_row = Adw.ActionRow(title="VTube Studio Prefix")
        self._update_vts_status()
        status_group.add(self.vts_row)

        self.vts_runtime_row = Adw.ActionRow(title="VTube Studio Process")
        self._update_vts_runtime_status()
        status_group.add(self.vts_runtime_row)

        self.proton_row = Adw.ActionRow(title="Proton Runtime")
        self._update_proton_status()
        status_group.add(self.proton_row)

        self.shoost_row = Adw.ActionRow(title="Shoost")
        self._update_shoost_status()
        status_group.add(self.shoost_row)

        self.spout2pw_row = Adw.ActionRow(title="spout2pw")
        self._update_spout2pw_status()
        status_group.add(self.spout2pw_row)

        self.obs_pwvideo_row = Adw.ActionRow(title="obs-pwvideo")
        self._update_obs_pwvideo_status()
        status_group.add(self.obs_pwvideo_row)

        inner.append(status_group)

        shoost_group = Adw.PreferencesGroup(title="Shoost")

        extract_row = Adw.ActionRow(
            title="Install Shoost",
            subtitle="Pick a Shoost zip and extract into VTS prefix",
        )
        self.extract_btn = Gtk.Button(label="Browse...", valign=Gtk.Align.CENTER)
        self.extract_btn.add_css_class("suggested-action")
        self.extract_btn.connect("clicked", self.on_extract_shoost)
        extract_row.add_suffix(self.extract_btn)
        extract_row.set_activatable_widget(self.extract_btn)
        shoost_group.add(extract_row)

        self.launch_row = Adw.ActionRow(
            title="Launch Shoost",
            subtitle="Runtime compatibility is checked continuously",
        )
        info_btn = Gtk.Button(icon_name="dialog-information-symbolic", valign=Gtk.Align.CENTER)
        info_btn.set_tooltip_text("Show terminal command")
        info_btn.connect("clicked", self.on_show_shoost_command)
        self.launch_row.add_suffix(info_btn)
        self.launch_btn = Gtk.Button(label="Launch", valign=Gtk.Align.CENTER)
        self.launch_btn.add_css_class("suggested-action")
        self.launch_btn.connect("clicked", self.on_launch_shoost)
        self.launch_row.add_suffix(self.launch_btn)
        self.launch_row.set_activatable_widget(self.launch_btn)
        shoost_group.add(self.launch_row)

        stop_row = Adw.ActionRow(
            title="Stop Shoost",
            subtitle="Kill the running Shoost process",
        )
        self.stop_btn = Gtk.Button(label="Stop", valign=Gtk.Align.CENTER)
        self.stop_btn.add_css_class("destructive-action")
        self.stop_btn.connect("clicked", self.on_stop_shoost)
        stop_row.add_suffix(self.stop_btn)
        stop_row.set_activatable_widget(self.stop_btn)
        shoost_group.add(stop_row)

        uninstall_row = Adw.ActionRow(
            title="Uninstall Shoost",
            subtitle=str(SHOOST_DIR),
        )
        self.uninstall_btn = Gtk.Button(label="Uninstall", valign=Gtk.Align.CENTER)
        self.uninstall_btn.add_css_class("destructive-action")
        self.uninstall_btn.connect("clicked", self.on_uninstall_shoost)
        uninstall_row.add_suffix(self.uninstall_btn)
        uninstall_row.set_activatable_widget(self.uninstall_btn)
        shoost_group.add(uninstall_row)

        inner.append(shoost_group)

        spout2pw_group = Adw.PreferencesGroup(
            title="spout2pw",
            description="Bridge Spout2 output to PipeWire for OBS",
        )

        vts_opts_row = Adw.ActionRow(
            title="VTube Studio Launch Options",
            subtitle="Set in Steam > VTube Studio > Properties > Launch Options",
        )
        self.vts_opts_info_btn = Gtk.Button(
            icon_name="dialog-information-symbolic", valign=Gtk.Align.CENTER
        )
        self.vts_opts_info_btn.set_tooltip_text("Show launch options")
        self.vts_opts_info_btn.connect("clicked", self.on_show_vts_launch_opts)
        vts_opts_row.add_suffix(self.vts_opts_info_btn)
        copy_opts_btn = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER)
        copy_opts_btn.set_tooltip_text("Copy launch options")
        copy_opts_btn.connect("clicked", self.on_copy_vts_launch_opts)
        vts_opts_row.add_suffix(copy_opts_btn)
        spout2pw_group.add(vts_opts_row)

        inner.append(spout2pw_group)

        obs_group = Adw.PreferencesGroup(
            title="OBS",
            description="obs-pwvideo plugin lets OBS read the PipeWire stream",
        )

        pwv_install_row = Adw.ActionRow(
            title="Install obs-pwvideo plugin",
            subtitle=f"Copy bundled plugin to {OBS_PWVIDEO_USER_DIR}",
        )
        self.pwv_install_btn = Gtk.Button(label="Install", valign=Gtk.Align.CENTER)
        self.pwv_install_btn.add_css_class("suggested-action")
        self.pwv_install_btn.connect("clicked", self.on_install_obs_pwvideo)
        pwv_install_row.add_suffix(self.pwv_install_btn)
        pwv_install_row.set_activatable_widget(self.pwv_install_btn)
        obs_group.add(pwv_install_row)

        pwv_remove_row = Adw.ActionRow(
            title="Remove obs-pwvideo plugin",
            subtitle=str(OBS_PWVIDEO_USER_DIR),
        )
        self.pwv_remove_btn = Gtk.Button(label="Remove", valign=Gtk.Align.CENTER)
        self.pwv_remove_btn.add_css_class("destructive-action")
        self.pwv_remove_btn.connect("clicked", self.on_remove_obs_pwvideo)
        pwv_remove_row.add_suffix(self.pwv_remove_btn)
        pwv_remove_row.set_activatable_widget(self.pwv_remove_btn)
        obs_group.add(pwv_remove_row)

        inner.append(obs_group)

        log_group = Adw.PreferencesGroup(title="Log")
        self.log_view = Gtk.TextView(editable=False, monospace=True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_buf = self.log_view.get_buffer()

        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=100)
        scroll.set_child(self.log_view)

        frame = Gtk.Frame()
        frame.set_child(scroll)
        log_group.add(frame)
        inner.append(log_group)

        return scrollable

    def _onboarding_step(self):
        """Return (icon, title, description) for the first failing onboarding
        check, or None when the user can proceed to the main view. Add future
        checks by extending this method — order = display order."""
        if not VTS_PREFIX.exists():
            return (
                "dialog-warning-symbolic",
                "VTS Prefix not found",
                "Go to Steam → Library → VTube Studio → Right-click → "
                "Properties → Compatibility → set Proton Experimental (or any "
                "fork ≥ 10.0). Click Retry after that.",
            )
        return None

    def _build_onboarding_view(self, step):
        icon, title, desc = step
        page = Adw.StatusPage(icon_name=icon, title=title, description=desc)
        retry_btn = Gtk.Button(label="Retry", halign=Gtk.Align.CENTER)
        retry_btn.add_css_class("suggested-action")
        retry_btn.add_css_class("pill")
        retry_btn.connect("clicked", self._on_retry_onboarding)
        page.set_child(retry_btn)
        return page

    def _apply_view(self):
        """Swap the body between onboarding and the main view based on the
        current onboarding state. Re-runnable; callers don't track which view
        is currently shown."""
        child = self._body_slot.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._body_slot.remove(child)
            child = nxt
        step = self._onboarding_step()
        if step:
            view = self._build_onboarding_view(step)
        else:
            self._update_vts_status()
            self._update_proton_status()
            view = self._main_view
        self._body_slot.append(view)

    def _on_retry_onboarding(self, _btn):
        self._apply_view()

    def _log(self, msg):
        end = self.log_buf.get_end_iter()
        self.log_buf.insert(end, msg + "\n")
        end = self.log_buf.get_end_iter()
        self.log_view.scroll_to_iter(end, 0, False, 0, 0)

    def _set_row_status(self, row, subtitle, ok):
        row.set_subtitle(subtitle)
        old = getattr(row, "_status_icon", None)
        if old:
            row.remove(old)
        icon = Gtk.Image.new_from_icon_name(
            "emblem-ok-symbolic" if ok else "dialog-warning-symbolic"
        )
        row.add_suffix(icon)
        row._status_icon = icon

    def _update_vts_status(self):
        if VTS_PREFIX.exists():
            self._set_row_status(self.vts_row, str(VTS_PREFIX), True)
        else:
            self._set_row_status(self.vts_row, "Not found", False)

    def _update_vts_runtime_status(self):
        safe, summary, _blocker = self.shoost_launch_runtime
        self._set_row_status(self.vts_runtime_row, summary, safe)

    def _poll_shoost_launch_runtime(self):
        state = inspect_shoost_launch_runtime()
        if state != self.shoost_launch_runtime:
            self.shoost_launch_runtime = state
            self._update_vts_runtime_status()
            self._update_buttons()
        return True

    def _update_proton_status(self):
        self.proton_path = detect_proton_from_config()
        name = self.proton_path.name if self.proton_path else "Not detected"
        ok = bool(self.proton_path and (self.proton_path / "files/bin/wine").exists())
        self._set_row_status(self.proton_row, name, ok)

    def _update_shoost_status(self):
        if self.shoost_exe:
            self._set_row_status(self.shoost_row, self.shoost_exe.name, True)
        else:
            self._set_row_status(self.shoost_row, "Not installed", False)

    def _update_spout2pw_status(self):
        if self.spout2pw_sh:
            self._set_row_status(self.spout2pw_row, str(self.spout2pw_sh.parent), True)
        else:
            self._set_row_status(self.spout2pw_row, "Not installed", False)

    def _update_obs_pwvideo_status(self):
        if is_obs_pwvideo_installed():
            self._set_row_status(self.obs_pwvideo_row, str(OBS_PWVIDEO_USER_DIR), True)
        else:
            self._set_row_status(self.obs_pwvideo_row, "Not installed", False)

    def _update_buttons(self):
        installed = self.shoost_exe is not None
        runtime_safe = self.shoost_launch_runtime[0]
        self.launch_btn.set_sensitive(
            installed and runtime_safe and self.shoost_proc is None
        )
        self.stop_btn.set_sensitive(self.shoost_proc is not None)
        self.uninstall_btn.set_sensitive(installed and self.shoost_proc is None)
        pwv_installed = is_obs_pwvideo_installed()
        self.pwv_install_btn.set_sensitive(self.bundled_obs_pwvideo is not None)
        self.pwv_install_btn.set_label("Reinstall" if pwv_installed else "Install")
        self.pwv_remove_btn.set_sensitive(pwv_installed)
        self._refresh_settings_dialog()

    def _refresh_settings_dialog(self):
        btn = getattr(self, "_settings_reset_btn", None)
        if btn:
            btn.set_sensitive(self.bundled_spout2pw is not None)

    def _get_vts_launch_opts(self):
        if self.spout2pw_sh:
            return f"{self.spout2pw_sh} %command%"
        return f"{SPOUT2PW_DIR}/spout2pw.sh %command%"

    def _show_copy_dialog(self, heading, body, copy_text=None):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading=heading,
            body=body,
        )
        dialog.add_response("copy", "Copy")
        dialog.add_response("close", "Close")
        dialog.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_copy_dialog_response, copy_text or body)
        dialog.present()

    def _on_copy_dialog_response(self, _dialog, response, text):
        if response == "copy":
            self.get_clipboard().set(text)
            self._log("Copied to clipboard")

    def on_extract_shoost(self, _btn):
        dialog = Gtk.FileDialog()
        dialog.set_title("Select Shoost zip")
        zip_filter = Gtk.FileFilter()
        zip_filter.set_name("Zip archives")
        zip_filter.add_pattern("*.zip")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(zip_filter)
        dialog.set_filters(filters)

        downloads = Path.home() / "Downloads"
        if downloads.exists():
            dialog.set_initial_folder(Gio.File.new_for_path(str(downloads)))

        dialog.open(self, None, self._on_shoost_zip_picked)

    def _on_shoost_zip_picked(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return

        zip_path = Path(file.get_path())
        self._log(f"Selected: {zip_path.name}")

        try:
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()

                top_dirs = {n.split("/")[0] for n in names if "/" in n}
                strip_prefix = (top_dirs.pop() + "/") if len(top_dirs) == 1 else ""

                exe_name = None
                for n in names:
                    rel = n[len(strip_prefix):] if strip_prefix else n
                    if rel.lower().endswith(".exe") and "shoost" in rel.lower() and "/" not in rel:
                        exe_name = rel
                        break

                if not exe_name:
                    self._log("ERROR: No Shoost .exe found in zip")
                    return

                if SHOOST_DIR.exists():
                    shutil.rmtree(SHOOST_DIR)
                    self._log("Removed old Shoost installation")

                SHOOST_DIR.mkdir(parents=True, exist_ok=True)

                for info in zf.infolist():
                    if strip_prefix and not info.filename.startswith(strip_prefix):
                        continue
                    rel = info.filename[len(strip_prefix):] if strip_prefix else info.filename
                    if not rel:
                        continue
                    dest = SHOOST_DIR / rel
                    if info.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)

                self.shoost_exe = SHOOST_DIR / exe_name
                self._log(f"Extracted {exe_name} to {SHOOST_DIR}")

        except Exception as e:
            self._log(f"ERROR: {e}")

        self._update_shoost_status()
        self._update_buttons()

    def on_show_shoost_command(self, _btn):
        if not self.shoost_exe:
            self._log("Shoost not installed")
            return
        self._poll_shoost_launch_runtime()
        cmd, _summary, blocker = build_shoost_launch_command(self.shoost_exe)
        if blocker:
            self._log(f"ERROR: {blocker}")
            self._show_copy_dialog("Shoost Launch Blocked", blocker, blocker)
            return
        self._show_copy_dialog("Terminal Command", shlex.join(cmd))

    def on_launch_shoost(self, _btn):
        if not self.shoost_exe or not self.shoost_exe.exists():
            self._log("ERROR: Shoost not installed")
            return
        self._poll_shoost_launch_runtime()
        cmd, summary, blocker = build_shoost_launch_command(self.shoost_exe)
        if blocker:
            self._log(f"ERROR: {blocker}")
            self._show_copy_dialog("Shoost Launch Blocked", blocker, blocker)
            return

        self._log(f"Launching {self.shoost_exe.name}: {summary}")

        try:
            self.shoost_proc = subprocess.Popen(
                cmd,
                cwd=str(SHOOST_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            self._log(f"Started (PID {self.shoost_proc.pid})")
            GLib.io_add_watch(
                GLib.IOChannel.unix_new(self.shoost_proc.stdout.fileno()),
                GLib.IOCondition.IN | GLib.IOCondition.HUP,
                self._on_proc_output,
            )
        except Exception as e:
            self._log(f"ERROR: {e}")
            self.shoost_proc = None

        self._update_buttons()

    def _on_proc_output(self, channel, condition):
        if condition & GLib.IOCondition.IN:
            try:
                line = channel.readline()
                if line:
                    self._log(line.rstrip("\n"))
                    return True
            except Exception:
                pass

        if self.shoost_proc:
            ret = self.shoost_proc.poll()
            self._log(f"Shoost exited (code {ret})")
            self.shoost_proc = None
            self._update_buttons()
        return False

    def on_stop_shoost(self, _btn):
        if not self.shoost_proc:
            return

        self._log("Stopping Shoost...")
        stop_cmd = build_shoost_stop_command(self.shoost_exe)
        if stop_cmd:
            try:
                result = subprocess.run(
                    stop_cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    self._log(result.stdout.strip() or result.stderr.strip())
            except (OSError, subprocess.TimeoutExpired) as e:
                self._log(f"ERROR: {e}")

        if self.shoost_proc.poll() is None:
            self.shoost_proc.terminate()
            try:
                self.shoost_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.shoost_proc.kill()
        self._log("Stopped")
        self.shoost_proc = None
        self._update_buttons()

    def on_uninstall_shoost(self, _btn):
        if not SHOOST_DIR.exists():
            self._log("Nothing to uninstall")
            return

        try:
            shutil.rmtree(SHOOST_DIR)
            self.shoost_exe = None
            self._log(f"Removed {SHOOST_DIR}")
        except Exception as e:
            self._log(f"ERROR: {e}")

        self._update_shoost_status()
        self._update_buttons()

    def _extract_spout2pw_tar(self, tar_path):
        try:
            with tarfile.open(tar_path, "r:gz") as tf:
                members = tf.getnames()

                top_dirs = {m.split("/")[0] for m in members if "/" in m}
                strip_prefix = (top_dirs.pop() + "/") if len(top_dirs) == 1 else ""

                sh_found = any(
                    (m[len(strip_prefix):] if strip_prefix else m) == "spout2pw.sh"
                    for m in members
                )
                if not sh_found:
                    self._log("ERROR: spout2pw.sh not found in tarball")
                    return

                if SPOUT2PW_DIR.exists():
                    shutil.rmtree(SPOUT2PW_DIR)

                SPOUT2PW_DIR.mkdir(parents=True, exist_ok=True)

                for member in tf.getmembers():
                    if strip_prefix and not member.name.startswith(strip_prefix):
                        continue
                    rel = member.name[len(strip_prefix):] if strip_prefix else member.name
                    if not rel:
                        continue

                    dest = SPOUT2PW_DIR / rel
                    if member.isdir():
                        dest.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with tf.extractfile(member) as src, open(dest, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        if member.mode & 0o111:
                            dest.chmod(dest.stat().st_mode | 0o111)

                self.spout2pw_sh = SPOUT2PW_DIR / "spout2pw.sh"
                self._log(f"Installed spout2pw to {SPOUT2PW_DIR}")
                self._log(f"Set VTS launch options to: {self._get_vts_launch_opts()}")

        except Exception as e:
            self._log(f"ERROR: {e}")

        self._update_spout2pw_status()
        self._update_buttons()

    def on_install_spout2pw(self, _btn):
        dialog = Gtk.FileDialog()
        dialog.set_title("Select spout2pw tarball")
        tar_filter = Gtk.FileFilter()
        tar_filter.set_name("Tarballs (.tar.gz)")
        tar_filter.add_pattern("*.tar.gz")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(tar_filter)
        dialog.set_filters(filters)

        downloads = Path.home() / "Downloads"
        if downloads.exists():
            dialog.set_initial_folder(Gio.File.new_for_path(str(downloads)))

        dialog.open(self, None, self._on_spout2pw_tar_picked)

    def _on_spout2pw_tar_picked(self, dialog, result):
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return

        tar_path = Path(file.get_path())
        self._log(f"Selected: {tar_path.name}")
        self._extract_spout2pw_tar(tar_path)

    def on_show_vts_launch_opts(self, _btn):
        opts = self._get_vts_launch_opts()
        self._show_copy_dialog(
            "VTube Studio Launch Options",
            f"Set this in Steam > VTube Studio > Properties > General > Launch Options:\n\n{opts}",
            copy_text=opts,
        )

    def on_copy_vts_launch_opts(self, _btn):
        opts = self._get_vts_launch_opts()
        self.get_clipboard().set(opts)
        self._log(f"Copied: {opts}")

    def on_reset_spout2pw(self, _btn):
        if not self.bundled_spout2pw:
            self._log("ERROR: No bundled spout2pw available")
            return
        try:
            extract_bundle_to_home()
            self._log(f"Reset spout2pw to bundle at {SPOUT2PW_DIR}")
        except Exception as e:
            self._log(f"ERROR: {e}")

        self.spout2pw_sh = find_spout2pw()
        self._update_spout2pw_status()
        self._update_buttons()

    def on_install_obs_pwvideo(self, _btn):
        if not self.bundled_obs_pwvideo:
            self._log("ERROR: No bundled obs-pwvideo available")
            return
        so = self.bundled_obs_pwvideo / "bin/64bit/obs-pwvideo.so"
        missing = check_obs_pwvideo_deps(so)
        if missing:
            body = (
                "The bundled plugin links libraries not present on this system:\n\n"
                + "\n".join(f"  • {m}" for m in missing)
                + "\n\nThis usually means your OBS major version differs from "
                "the prebuilt's. OBS won't load the plugin. Install obs-pwvideo "
                "from your distro package manager / AUR instead, or build it "
                "from source.\n\nInstall the bundled plugin anyway?"
            )
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="ABI mismatch",
                body=body,
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("install", "Install Anyway")
            dialog.set_response_appearance("install", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.connect("response", self._on_pwv_install_confirm)
            dialog.present()
            return
        self._do_install_obs_pwvideo()

    def _on_pwv_install_confirm(self, _dialog, response):
        if response == "install":
            self._do_install_obs_pwvideo()

    def _do_install_obs_pwvideo(self):
        src = self.bundled_obs_pwvideo
        try:
            if OBS_PWVIDEO_USER_DIR.exists():
                shutil.rmtree(OBS_PWVIDEO_USER_DIR)
            shutil.copytree(src, OBS_PWVIDEO_USER_DIR)
            self._log(f"Installed obs-pwvideo to {OBS_PWVIDEO_USER_DIR}")
            self._log("Restart OBS to pick up the new plugin.")
        except Exception as e:
            self._log(f"ERROR: {e}")
        self._update_obs_pwvideo_status()
        self._update_buttons()

    def on_remove_obs_pwvideo(self, _btn):
        if not OBS_PWVIDEO_USER_DIR.exists():
            self._log("obs-pwvideo not installed")
            return
        try:
            shutil.rmtree(OBS_PWVIDEO_USER_DIR)
            self._log(f"Removed {OBS_PWVIDEO_USER_DIR}")
        except Exception as e:
            self._log(f"ERROR: {e}")
        self._update_obs_pwvideo_status()
        self._update_buttons()

    def _vhelper_install_paths(self):
        p = self.install_prefix
        return [
            p / "bin/vhelper",
            p / "share/vhelper",
            p / "share/applications/com.vhelper.app.desktop",
            p / "share/icons/hicolor/scalable/apps/com.vhelper.app.svg",
        ]

    def _user_data_paths(self):
        """User-owned bundle copies vhelper drops outside the install prefix.
        Always user-writable, so removal never needs pkexec."""
        return [SPOUT2PW_DIR, OBS_PWVIDEO_USER_DIR]

    def on_uninstall_vhelper(self, _btn):
        system_existing = [p for p in self._vhelper_install_paths() if p.exists()]
        user_existing = [p for p in self._user_data_paths() if p.exists()]
        existing = system_existing + user_existing
        if not existing:
            self._log("Nothing to uninstall")
            return
        files_list = "\n".join(f"  • {p}" for p in existing)
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Uninstall vhelper?",
            body=(
                f"This will remove:\n\n{files_list}\n\n"
                "Shoost is not affected."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("uninstall", "Uninstall")
        dialog.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.connect(
            "response",
            self._on_uninstall_vhelper_response,
            system_existing,
            user_existing,
        )
        dialog.present()

    def on_open_settings(self, _btn):
        dialog = Adw.PreferencesDialog()
        dialog.set_title("Settings")
        page = Adw.PreferencesPage()
        dialog.add(page)

        s2p_group = Adw.PreferencesGroup(
            title="spout2pw",
            description=(
                "Installed at "
                f"{SPOUT2PW_DIR}. Steam pressure-vessel only mounts $HOME "
                "into the Proton sandbox, so it must live here."
            ),
        )

        install_row = Adw.ActionRow(
            title="Install from file",
            subtitle="Replace with a local .tar.gz",
        )
        install_btn = Gtk.Button(label="Install", valign=Gtk.Align.CENTER)
        install_btn.connect("clicked", self.on_install_spout2pw)
        install_row.add_suffix(install_btn)
        install_row.set_activatable_widget(install_btn)
        s2p_group.add(install_row)

        reset_row = Adw.ActionRow(
            title="Reset to bundle",
            subtitle="Restore the vendored spout2pw shipped with vhelper",
        )
        self._settings_reset_btn = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER)
        self._settings_reset_btn.add_css_class("destructive-action")
        self._settings_reset_btn.connect("clicked", self.on_reset_spout2pw)
        reset_row.add_suffix(self._settings_reset_btn)
        reset_row.set_activatable_widget(self._settings_reset_btn)
        s2p_group.add(reset_row)

        page.add(s2p_group)

        if self.install_prefix:
            vh_group = Adw.PreferencesGroup(
                title="vhelper",
                description=f"Installed at {self.install_prefix}",
            )
            uninstall_vh_row = Adw.ActionRow(
                title="Uninstall vhelper",
                subtitle="Removes the app, spout2pw, and obs-pwvideo plugin. Shoost stays.",
            )
            un_btn = Gtk.Button(label="Uninstall", valign=Gtk.Align.CENTER)
            un_btn.add_css_class("destructive-action")
            un_btn.connect("clicked", self.on_uninstall_vhelper)
            uninstall_vh_row.add_suffix(un_btn)
            uninstall_vh_row.set_activatable_widget(un_btn)
            vh_group.add(uninstall_vh_row)
            page.add(vh_group)

        dialog.connect("closed", self._on_settings_closed)
        self._refresh_settings_dialog()
        dialog.present(self)

    def _on_settings_closed(self, _dialog):
        self._settings_reset_btn = None

    def _on_uninstall_vhelper_response(self, _dialog, response, system_existing, user_existing):
        if response != "uninstall":
            return

        needs_root = system_existing and not os.access(self.install_prefix, os.W_OK)
        self._log("Uninstalling vhelper...")

        try:
            if needs_root:
                if not shutil.which("pkexec"):
                    self._log("ERROR: pkexec not available; cannot remove root-owned files")
                    return
                result = subprocess.run(
                    ["pkexec", "rm", "-rf", *[str(p) for p in system_existing]],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    self._log(f"ERROR: {(result.stderr or result.stdout).strip()}")
                    return
            else:
                for p in system_existing:
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
            for p in user_existing:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
            self._log("Removed. Quitting...")
            GLib.timeout_add(800, lambda: (self.get_application().quit(), False)[1])
        except Exception as e:
            self._log(f"ERROR: {e}")


class VHelperApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="com.vhelper.app",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )

    def do_activate(self):
        win = self.get_active_window()
        if not win:
            win = VHelperWindow(self)
        win.present()


def main():
    app = VHelperApp()
    app.run()


if __name__ == "__main__":
    main()
