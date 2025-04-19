#!/usr/bin/env python3
"""
Stream webcam video to Twitch and periodically post Klipper status updates
in chat and update stream title for a Creality K1 printer.
"""
import os
import json
import time
import socket
import subprocess
import threading
import requests

# Path to configuration file
CONFIG_PATH = os.path.expanduser("~/.printer_status/config.json")
TWITCH_HOST = "irc.chat.twitch.tv"
TWITCH_PORT = 6667


def load_config():
    """
    Load JSON configuration from CONFIG_PATH.

    Returns:
        dict: Configuration dictionary.
    """
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)


def save_config(cfg):
    """
    Save updated configuration back to CONFIG_PATH.

    Args:
        cfg (dict): Configuration dictionary to save.
    """
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)


def format_token(tok):
    """
    Ensure OAuth token has the 'oauth:' prefix required by Twitch IRC.

    Args:
        tok (str): Raw access token.
    Returns:
        str: Prefixed token.
    """
    return tok if tok.startswith("oauth:") else "oauth:" + tok


def refresh_access_token(cfg):
    """
    Refresh the Twitch API access token using the refresh token.

    Args:
        cfg (dict): Configuration containing client_id, client_secret, and refresh_token.
    Returns:
        str or None: New access_token if successful, else None.
    """
    url = "https://id.twitch.tv/oauth2/token"
    data = {
        "client_id":     cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "grant_type":    "refresh_token",
        "refresh_token": cfg["refresh_token"]
    }
    r = requests.post(url, data=data)
    if not r.ok:
        print("❌ Token refresh failed:", r.status_code, r.text)
        return None
    res = r.json()
    at = res.get("access_token")
    rt = res.get("refresh_token")
    if at and rt:
        cfg["access_token"] = at
        cfg["refresh_token"] = rt
        save_config(cfg)
        print("🔄 Access token refreshed")
        return at
    print("❌ Refresh response missing tokens:", res)
    return None


def get_broadcaster_id(cfg):
    """
    Retrieve the Twitch broadcaster ID for the configured login.
    Refreshes token if a 401 Unauthorized is received.

    Args:
        cfg (dict): Configuration with broadcaster_login, client_id, and access_token.
    Returns:
        str or None: Broadcaster ID if found, else None.
    """
    url = f"https://api.twitch.tv/helix/users?login={cfg['broadcaster_login']}"
    headers = {
        "Client-ID": cfg["client_id"],
        "Authorization": f"Bearer {cfg['access_token']}"
    }
    r = requests.get(url, headers=headers)
    if r.status_code == 401:
        new = refresh_access_token(cfg)
        if not new:
            return None
        headers["Authorization"] = f"Bearer {new}"
        r = requests.get(url, headers=headers)
    if not r.ok:
        print("❌ Cannot fetch broadcaster ID:", r.status_code, r.text)
        return None
    data = r.json().get("data", [])
    return data[0]["id"] if data else None


def get_key(d, target):
    """
    Case-insensitive lookup in a dictionary.

    Args:
        d (dict): Dictionary to search.
        target (str): Key to find (case-insensitive).
    Returns:
        Any: Value if found, else {}.
    """
    tn = target.strip().lower()
    for k, v in d.items():
        if k.strip().lower() == tn:
            return v
    return {}


def extract_pin_value(pin):
    """
    Convert an 'output_pin' JSON object to a percentage string.

    Args:
        pin (dict): JSON object with 'value' between 0 and 1.
    Returns:
        str: Percentage string like '75%'.
    """
    try:
        return f"{int(float(pin.get('value', 0)) * 100)}%"
    except:
        return "N/A"


def get_klipper_status(url):
    """
    Query the Klipper/Moonraker API for detailed printer status.
    Handles completed prints and missing file path gracefully.

    Args:
        url (str): Moonraker API endpoint.
    Returns:
        str: Formatted status line for chat and title.
    """
    try:
        j = requests.get(url, timeout=3).json()["result"]["status"]
        # File and state
        ps = j.get("print_stats", {})
        vsd = j.get("virtual_sdcard", {})
        file_path = vsd.get("file_path") or "<none>"
        filename = file_path.split("/")[-1]
        state = ps.get("state", "unknown")

        # Progress and timing
        prog = j.get("display_status", {}).get("progress", 0.0)
        progress_pct = int(prog * 100)
        print_sec = ps.get("print_duration", 0.0)
        hrs, rem = divmod(print_sec, 3600)
        mins = rem // 60
        elapsed = f"{int(hrs)}h{int(mins):02d}m" if hrs else f"{int(mins)}m"
        eta = "??"
        if prog > 0:
            total = print_sec / prog
            th, trem = divmod(total, 3600)
            tm = trem // 60
            eta = f"{int(th)}h{int(tm):02d}m" if th else f"{int(tm)}m"

        # Temperatures
        extr = j.get("extruder", {})
        bed = j.get("heater_bed", {})
        et = extr.get("temperature", "?")
        ett = extr.get("target", "?")
        bt = bed.get("temperature", "?")
        btt = bed.get("target", "?")
        extr_state = ""
        if isinstance(et, (int, float)) and isinstance(ett, (int, float)):
            if et < ett - 1:
                extr_state = " (heating)"
            elif et > ett + 1:
                extr_state = " (cooling)"
        bed_state = ""
        if isinstance(bt, (int, float)) and isinstance(btt, (int, float)):
            if bt < btt - 1:
                bed_state = " (heating)"
            elif bt > btt + 1:
                bed_state = " (cooling)"
        temps = f"🔥 Hotend: {et}/{ett}°C{extr_state}  🛏️ Bed: {bt}/{btt}°C{bed_state}"

        # Sensors
        mcu_temp = get_key(j, "temperature_sensor mcu_temp").get("temperature", "?")
        ambient_temp = get_key(j, "temperature_sensor chamber_temp").get("temperature", "?")
        sensors = f"💻 MCU: {mcu_temp}°C  🌡️ Ambient: {ambient_temp}°C"

        # Fans
        hf_speed = get_key(j, "heater_fan hotend_fan").get("speed")
        hotend_fan = "On" if hf_speed == 1 else "Off" if hf_speed == 0 else "N/A"
        fan0 = extract_pin_value(get_key(j, "output_pin fan0"))
        fan1 = extract_pin_value(get_key(j, "output_pin fan1"))
        fan2 = extract_pin_value(get_key(j, "output_pin fan2"))
        fans = f"❄️ Hotend fan: {hotend_fan} | 🆒 Case fans: fan0 {fan0}, fan1 {fan1}, fan2 {fan2}"

        # Position
        pos = j.get("toolhead", {}).get("position", [None, None, None, None])
        if None not in pos[:3]:
            pos_str = f"📍 X{pos[0]:.0f} Y{pos[1]:.0f} Z{pos[2]:.2f}"
        else:
            pos_str = "📍 N/A"

        # Layer
        layer = vsd.get("layer")
        layer_count = vsd.get("layer_count")
        layer_str = f"🧱 Layer: {layer}/{layer_count}" if layer is not None and layer_count else "🧱 Layer: N/A"

        # Speed
        speed_factor = j.get("gcode_move", {}).get("speed_factor", 1.0)
        speed_pct = int(speed_factor * 100)

        status_message = (
            f"📁 File: {filename} | "
            f"🖨️ State: {state} | "
            f"📊 Progress: {progress_pct}% | "
            f"⏱️ Time: {elapsed}/{eta} | "
            f"{temps} | "
            f"{sensors} | "
            f"{fans} | "
            f"{pos_str} | "
            f"{layer_str} | "
            f"🏎️ Speed: {speed_pct}%"
        )
        return status_message
    except Exception as e:
        return f"⚠️ Klipper API error: {e}"


def make_creative_title(status):
    """
    Convert the full status line into a concise Twitch stream title (<=140 chars).

    Args:
        status (str): Full status line from get_klipper_status().
    Returns:
        str: Shortened title.
    """
    parts = status.split("|")
    if len(parts) < 5:
        return ""
    file_part = parts[0].split(":", 1)[-1].strip()
    state_part = parts[1].split(":", 1)[-1].strip()
    progress_part = parts[2].split(":", 1)[-1].strip()
    elapsed = parts[3].split("|", 1)[0].split(":", 1)[-1].split("/")[0].strip()
    temps_part = parts[4]
    title = (
        f"🖨️{file_part} | 🚀{progress_part} | ⏰{elapsed} | {temps_part} | ✅{state_part}"
    )
    return title[:137] + "..." if len(title) > 140 else title


def update_title(cfg, broadcaster_id, title):
    """
    Update the Twitch stream title via the Helix API.

    Args:
        cfg (dict): Config with client_id and access_token.
        broadcaster_id (str): Twitch user ID.
        title (str): New stream title.
    """
    url = f"https://api.twitch.tv/helix/channels?broadcaster_id={broadcaster_id}"
    headers = {
        "Client-ID": cfg["client_id"],
        "Authorization": f"Bearer {cfg['access_token']}"
    }
    requests.patch(url, headers=headers, json={"title": title})


def connect_chat(nick, tok, channel):
    """
    Connect to Twitch IRC chat.

    Args:
        nick (str): Twitch username.
        tok (str): OAuth token (with or without prefix).
        channel (str): Channel name to join.
    Returns:
        socket.socket: Connected IRC socket.
    """
    s = socket.socket()
    s.connect((TWITCH_HOST, TWITCH_PORT))
    s.send(f"PASS {format_token(tok)}\r\n".encode())
    s.send(f"NICK {nick}\r\n".encode())
    s.send(f"JOIN #{channel}\r\n".encode())
    time.sleep(1)
    return s


def chat_worker(cfg):
    """
    Worker thread: periodically send full printer status to chat.
    Automatically recovers from errors.
    """
    chan = cfg["broadcaster_login"]
    sock = connect_chat(chan, cfg["access_token"], chan)
    interval = cfg.get("chat_interval", cfg.get("update_interval", 60))
    while True:
        try:
            msg = get_klipper_status(cfg["klipper_api_url"])
            sock.send(f"PRIVMSG #{chan} :{msg}\r\n".encode())
            print(f"[chat_worker] Sent to chat: {msg}")
        except Exception as e:
            print(f"[chat_worker] Error: {e}, retrying in 5s")
            time.sleep(5)
            continue
        time.sleep(interval)


def title_worker(cfg):
    """
    Worker thread: periodically update Twitch stream title.
    Skips invalid status formats.
    """
    broadcaster_id = get_broadcaster_id(cfg)
    if not broadcaster_id:
        print("[title_worker] No broadcaster ID, exiting thread.")
        return
    interval = cfg.get("title_interval", cfg.get("update_interval", 60))
    while True:
        try:
            msg = get_klipper_status(cfg["klipper_api_url"])
            title = make_creative_title(msg)
            if title:
                update_title(cfg, broadcaster_id, title)
                print(f"[title_worker] Updated title: {title}")
        except Exception as e:
            print(f"[title_worker] Error: {e}, retrying in 5s")
            time.sleep(5)
            continue
        time.sleep(interval)


def ffmpeg_worker(cfg):
    """
    Stream local webcam to Twitch via FFmpeg, logging stats every 30s.
    """
    ff = cfg["ffmpeg"]
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-f", cfg["ffmpeg"].get("format", "v4l2"),
        "-fflags", "+genpts",
        "-video_size", cfg["ffmpeg"].get("video_size", "640x480"),
        "-input_format", cfg["ffmpeg"].get("input_format", "h264"),
        "-i", cfg["ffmpeg"]["device"],
        "-c", "copy",
        "-f", "flv",
        f"rtmp://live.twitch.tv/app/{cfg['ffmpeg']['stream_key']}"
    ]
    subprocess.run(cmd, check=True)


def main():
    """
    Entry point: load config, start chat & title threads, then run FFmpeg.
    """
    cfg = load_config()
    threading.Thread(target=chat_worker, args=(cfg,), daemon=True).start()
    threading.Thread(target=title_worker, args=(cfg,), daemon=True).start()
    try:
        ffmpeg_worker(cfg)
    except KeyboardInterrupt:
        print("Interrupted, exiting.")
    except subprocess.CalledProcessError as e:
        print("FFmpeg error:", e)


if __name__ == "__main__":
    main()