#!/usr/bin/env python1
"""
BLE LED Controller using persistent gatttool hybrid approach for ELK-BLEDOM controllers.
This script directly uses 'gatttool' for sending commands, which is often more
robust for quirky devices when 'bluepy' write operations fail. 
REVISED FOR STABILITY to prevent connection hammering.
REVISED ALSO FOR security storing sensitive config info in secrets.
"""

import subprocess
import paho.mqtt.client as mqtt
import time
import json
import logging
import random
import threading
import signal
import sys
import re
import yaml # <-- Import the YAML library

# --- Configuration (will be loaded from secrets.yaml) ---
CONFIG = {}

# Global characteristic handle
CHAR_HANDLE = None

# Global state
current_state = "OFF"
current_brightness_ha = 255
current_r, current_g, current_b = 255, 255, 255
client = None
connection_lock = threading.Lock()
last_command_time = 0
last_keepalive_time = 0
KEEPALIVE_INTERVAL = 10

# GATTTOOL interactive process handle
gatttool_proc = None

# Commands
OFF_COMMAND_BYTES_HEX = "7e0404000000ff00ef"
POWER_ON_COMMAND_HEX = "7e0404010000ff00ef"
KEEPALIVE_COMMAND_HEX = "7e0400000000ff00ef"

# Connection settings
GATTTOOL_TIMEOUT = 5
COMMAND_DELAY = 0.1
RETRY_DELAY = 1

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO, # Changed to INFO for production
    format='%(asctime)s - %(levelname)s - %(message)s',
)

# --- All functions from your previous script go here ---
# (get_ble_color_command, discover_characteristic_handle, connect_gatttool_interactive,
# send_gatttool_interactive_command, test_device_connection, send_keepalive,
# on_connect, on_message, publish_state, signal_handler, schedule_keepalive)
# For brevity, I'm omitting the functions that don't change.
# Paste all of your existing functions here. The only function that
# needs to be created from scratch is the main() function below.

def get_ble_color_command(red: int, green: int, blue: int, brightness: int = 255) -> str:
    red = max(0, min(255, red))
    green = max(0, min(255, green))
    blue = max(0, min(255, blue))
    brightness = max(0, min(255, brightness))
    return f"7e070503{red:02x}{green:02x}{blue:02x}{brightness:02x}ef"

def discover_characteristic_handle() -> str | None:
    global CHAR_HANDLE
    logging.info(f"Discovering characteristic handle...")
    try:
        cmd = [
            'gatttool', '-b', CONFIG['device_mac'], '-t', 'public',
            '--characteristics'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=GATTTOOL_TIMEOUT)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "0000fff3-0000-1000-8000-00805f9b34fb" in line:
                    match = re.search(r'handle = (0x[0-9a-fA-F]+),', line)
                    if match:
                        CHAR_HANDLE = match.group(1)
                        logging.info(f"Found characteristic handle: {CHAR_HANDLE}")
                        return CHAR_HANDLE
            logging.error("Characteristic UUID not found.")
            return None
        else:
            logging.error(f"gatttool characteristics command failed: {result.stderr.strip()}")
            return None
    except Exception as e:
        logging.error(f"Error discovering characteristic handle: {e}")
        return None

def connect_gatttool_interactive() -> bool:
    global gatttool_proc
    logging.info("Attempting to establish interactive gatttool connection...")
    if gatttool_proc and gatttool_proc.poll() is None:
        gatttool_proc.terminate()
        gatttool_proc.wait(timeout=1)
    
    try:
        gatttool_proc = subprocess.Popen(
            ['gatttool', '-b', CONFIG['device_mac'], '-t', 'public', '--interactive'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        gatttool_proc.stdin.write("connect\n")
        gatttool_proc.stdin.flush()
        for line in iter(gatttool_proc.stdout.readline, ''):
            if "Connection successful" in line:
                logging.info("Interactive gatttool connection established.")
                return True
            if "error" in line.lower():
                logging.error(f"Gatttool connection failed: {line.strip()}")
                return False
        return False
    except Exception as e:
        logging.error(f"Error starting gatttool in interactive mode: {e}")
        return False

def send_gatttool_interactive_command(command_hex: str, retries: int = 3) -> bool:
    global gatttool_proc, last_command_time
    if not CHAR_HANDLE:
        return False
    if not connect_gatttool_interactive():
        return False
    with connection_lock:
        for attempt in range(retries):
            try:
                cmd_to_send = f"char-write-cmd {CHAR_HANDLE} {command_hex}\n"
                gatttool_proc.stdin.write(cmd_to_send)
                gatttool_proc.stdin.flush()
                # A simple sleep is often enough to wait for command completion
                time.sleep(0.5) 
                logging.info(f"Interactive command sent successfully: {command_hex}")
                last_command_time = time.time()
                return True
            except Exception as e:
                logging.error(f"Error sending interactive command on attempt {attempt + 1}: {e}")
                if attempt < retries - 1:
                    time.sleep(RETRY_DELAY)
    return False

send_gatttool_command = send_gatttool_interactive_command

def send_keepalive():
    global last_keepalive_time
    if time.time() - last_keepalive_time > KEEPALIVE_INTERVAL:
        logging.info("Sending keepalive command.")
        if current_state == "ON":
            cmd = get_ble_color_command(current_r, current_g, current_b, current_brightness_ha)
            if send_gatttool_command(cmd, retries=1):
                last_keepalive_time = time.time()
        else:
            if send_gatttool_command(KEEPALIVE_COMMAND_HEX, retries=1):
                last_keepalive_time = time.time()

def on_connect(client_instance, userdata, flags, rc, properties=None):
    if rc == 0:
        logging.info("Connected to MQTT broker successfully")
        client_instance.subscribe(f"{CONFIG['base_topic']}/light/set")
        publish_state()
    else:
        logging.error(f"Failed to connect to MQTT broker with code {rc}")

def on_message(client_instance, userdata, msg):
    global current_state, current_brightness_ha, current_r, current_g, current_b
    payload = msg.payload.decode('utf-8')
    logging.info(f"Received MQTT message: {msg.topic} = {payload}")
    try:
        command_data = json.loads(payload)
        if "state" in command_data:
            current_state = command_data["state"].upper()
        if "brightness" in command_data:
            current_brightness_ha = max(0, min(255, command_data["brightness"]))
        if "color" in command_data:
            color_data = command_data["color"]
            current_r = max(0, min(255, color_data.get('r', current_r)))
            current_g = max(0, min(255, color_data.get('g', current_g)))
            current_b = max(0, min(255, color_data.get('b', current_b)))
        
        if current_state == "ON":
            cmd = get_ble_color_command(current_r, current_g, current_b, current_brightness_ha)
            send_gatttool_command(cmd)
        elif current_state == "OFF":
            send_gatttool_command(OFF_COMMAND_BYTES_HEX)
        
        publish_state()
    except Exception as e:
        logging.error(f"Error processing MQTT message: {e}")

def publish_state():
    state_payload = {
        "state": current_state,
        "brightness": current_brightness_ha,
        "color": {"r": current_r, "g": current_g, "b": current_b}
    }
    if client and client.is_connected():
        client.publish(f"{CONFIG['base_topic']}/light/state", json.dumps(state_payload), retain=True)
        logging.info(f"Published state: {state_payload}")

def signal_handler(signum, frame):
    logging.info("Shutting down gracefully...")
    if gatttool_proc:
        gatttool_proc.terminate()
    if client:
        client.disconnect()
    sys.exit(0)

def schedule_keepalive(interval):
    while True:
        send_keepalive()
        time.sleep(interval)

# --- Main Execution ---
def main():
    """Main program execution."""
    global client, CONFIG

    # *** NEW: Load configuration from secrets file ***
    try:
        with open('secrets.yaml', 'r') as f:
            CONFIG = yaml.safe_load(f)
            # Add a default base_topic if not in secrets
            CONFIG.setdefault('base_topic', 'bedframe') 
    except FileNotFoundError:
        logging.critical("CRITICAL: secrets.yaml not found. Please create it.")
        return
    except Exception as e:
        logging.critical(f"CRITICAL: Could not read or parse secrets.yaml: {e}")
        return

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if not discover_characteristic_handle():
        logging.critical("Failed to discover characteristic handle. Cannot proceed.")
        return

    # Set up MQTT
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if CONFIG.get('mqtt_username') and CONFIG.get('mqtt_password'):
        client.username_pw_set(CONFIG['mqtt_username'], CONFIG['mqtt_password'])
    
    client.on_connect = on_connect
    client.on_message = on_message
    
    try:
        client.connect(CONFIG['mqtt_broker'], MQTT_PORT, MQTT_KEEP_ALIVE)
        
        keepalive_thread = threading.Thread(target=schedule_keepalive, args=(KEEPALIVE_INTERVAL,))
        keepalive_thread.daemon = True
        keepalive_thread.start()
        
        client.loop_forever()
    except Exception as e:
        logging.error(f"MQTT or main loop error: {e}")
    finally:
        if client:
            client.disconnect()
        if gatttool_proc:
            gatttool_proc.terminate()

if __name__ == "__main__":
    main()
