#!/usr/bin/env python1
"""
BLE LED Controller using persistent gatttool hybrid approach for ELK-BLEDOM controllers.
This script directly uses 'gatttool' for sending commands, which is often more
robust for quirky devices when 'bluepy' write operations fail. 
REVISED FOR STABILITY to prevent connection hammering.
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
import re # Required for regex

# Configure logging
logging.basicConfig(
    level=logging.DEBUG, # KEEP AS DEBUG FOR THIS RUN TO CONFIRM FIX
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/tmp/ble_led_gatttool.log'),
        logging.StreamHandler()
    ]
)

# --- Configuration ---
MQTT_BROKER_IP = "rberrypie3.local"
MQTT_PORT = 1883
MQTT_KEEP_ALIVE = 60
BASE_TOPIC = "bedframe"
MQTT_USERNAME = "ble_bridge_user"
MQTT_PASSWORD = "pw4_ha_vm_user"

# Bluetooth Configuration
DEVICE_MAC = "BE:67:00:5B:04:4A"
DEVICE_ADDR_TYPE = "public"
CHAR_UUID = "0000fff3-0000-1000-8000-00805f9b34fb" # Characteristic UUID for commands (fff3)
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb" # Service UUID (fff0)

# Global characteristic handle (will be discovered at startup)
CHAR_HANDLE = None

# Global state
current_state = "OFF"
current_brightness_ha = 255
current_r, current_g, current_b = 255, 255, 255
client = None
connection_lock = threading.Lock() # Ensures only one gatttool process runs at a time
last_command_time = 0 # Initialize last command time
last_keepalive_time = 0 # Initialize last keepalive time
KEEPALIVE_INTERVAL = 10 # Even shorter keepalive interval

# GATTTOOL interactive process handle
gatttool_proc = None

# Commands (standard ELK-BLEDOM byte sequences)
OFF_COMMAND_BYTES_HEX = "7e0404000000ff00ef"
POWER_ON_COMMAND_HEX = "7e0404010000ff00ef"
KEEPALIVE_COMMAND_HEX = "7e0400000000ff00ef" # This is often a 'query status' command

# Connection settings
GATTTOOL_TIMEOUT = 5 # Timeout for gatttool commands in seconds (including interactive connection)
COMMAND_DELAY = 0.1 # Delay between commands for interactive mode
RETRY_DELAY = 1 # Shorter delay between retry attempts for connection/command

def get_ble_color_command(red: int, green: int, blue: int, brightness: int = 255) -> str:
    """
    Generates the BLE command hex string for an ELK-BLEDOM controller.
    Ensures values are within 0-255 range.
    """
    red = max(0, min(255, red))
    green = max(0, min(255, green))
    blue = max(0, min(255, blue))
    brightness = max(0, min(255, brightness))
    return f"7e070503{red:02x}{green:02x}{blue:02x}{brightness:02x}ef"

def discover_characteristic_handle() -> str | None:
    """
    Discovers the characteristic handle for CHAR_UUID using gatttool.
    Uses the determined DEVICE_ADDR_TYPE ('public').
    """
    global CHAR_HANDLE
    
    logging.info(f"Discovering characteristic handle for UUID {CHAR_UUID}...")
    try:
        cmd = [
            'gatttool', '-b', DEVICE_MAC, '-t', DEVICE_ADDR_TYPE,
            '--characteristics'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=GATTTOOL_TIMEOUT)
        
        if result.returncode == 0:
            logging.debug(f"Full gatttool characteristics output:\n{result.stdout}")
            lines = result.stdout.splitlines() # Use splitlines() for robust newline handling
            
            # Pre-normalize the target UUID for comparison once
            normalized_target_uuid = CHAR_UUID.encode('ascii', 'ignore').decode('ascii').strip().lower()

            # Define the exact key strings we are looking for
            HANDLE_KEY = "char value handle ="
            UUID_KEY = "uuid ="

            for line in lines:
                logging.debug(f"Processing line (repr): {repr(line)}")
                
                # Aggressively normalize the current line to assist with string finding
                clean_line = line.encode('ascii', 'ignore').decode('ascii').strip()
                
                handle_value = None
                uuid_value = None

                # Search for the handle key
                handle_key_start = clean_line.find(HANDLE_KEY)
                if handle_key_start != -1:
                    # Extract string starting after the key
                    handle_data_start = handle_key_start + len(HANDLE_KEY)
                    # Find the end of the handle value (before the next comma or end of line)
                    handle_data_end = clean_line.find(',', handle_data_start)
                    if handle_data_end == -1: # If no comma, assume it's the end of the line
                        handle_data_end = len(clean_line)
                    handle_value = clean_line[handle_data_start:handle_data_end].strip()
                    logging.debug(f"  Found potential handle: '{handle_value}'")

                # Search for the UUID key
                uuid_key_start = clean_line.find(UUID_KEY)
                if uuid_key_start != -1:
                    # Extract string starting after the key to the end of the line
                    uuid_data_start = uuid_key_start + len(UUID_KEY)
                    uuid_value = clean_line[uuid_data_start:].strip()
                    logging.debug(f"  Found potential UUID: '{uuid_value}'")
                
                if handle_value and uuid_value:
                    # Normalize the found UUID for comparison
                    normalized_found_uuid = uuid_value.lower()
                    
                    logging.debug(f"  Comparing found UUID '{normalized_found_uuid}' with target '{normalized_target_uuid}'")
                    
                    if normalized_found_uuid == normalized_target_uuid:
                        CHAR_HANDLE = handle_value
                        logging.info(f"Found characteristic handle: {handle_value} for UUID {CHAR_UUID}")
                        return handle_value
                else:
                    logging.debug(f"Line did not contain both '{HANDLE_KEY}' and '{UUID_KEY}' in expected format, or extraction failed: {repr(line)}")
            
            logging.error(f"Characteristic UUID {CHAR_UUID} not found in gatttool services output after processing all lines.")
            return None
        else:
            logging.error(f"gatttool characteristics command failed: {result.stderr.strip()}")
            return None
            
    except subprocess.TimeoutExpired:
        logging.error(f"gatttool characteristics command timed out after {GATTTOOL_TIMEOUT} seconds.")
        return None
    except Exception as e:
        logging.error(f"Error discovering characteristic handle: {e}")
        return None

def connect_gatttool_interactive() -> bool:
    """Establishes a persistent interactive gatttool connection."""
    global gatttool_proc
    logging.info("Attempting to establish interactive gatttool connection...")
    
    # Terminate any existing gatttool process before starting a new one
    if gatttool_proc and gatttool_proc.poll() is None:
        logging.debug("Terminating existing gatttool process before new connection attempt.")
        try:
            gatttool_proc.stdin.write("exit\n")
            gatttool_proc.stdin.flush()
        except BrokenPipeError:
            pass # Already closed
        gatttool_proc.terminate()
        gatttool_proc.wait(timeout=1)
    
    for attempt in range(5): # Increased retries for connection attempts
        try:
            # Start gatttool in interactive mode
            gatttool_proc = subprocess.Popen(
                ['gatttool', '-b', DEVICE_MAC, '-t', DEVICE_ADDR_TYPE, '--interactive'],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True, # Use text mode for stdin/stdout
                bufsize=1 # Line-buffered output
            )
            
            # Send 'connect' command
            logging.debug("Sending 'connect' command to gatttool interactive session.")
            gatttool_proc.stdin.write("connect\n")
            gatttool_proc.stdin.flush()

            # Read output until "Connection successful" and the prompt '[LE]>'
            output_buffer = []
            start_time = time.time()
            prompt_received = False
            connection_success_reported = False

            while True:
                line = gatttool_proc.stdout.readline()
                if not line: # EOF, process terminated
                    logging.error(f"Gatttool process terminated unexpectedly during connection attempt. Output: {''.join(output_buffer)}")
                    break
                output_buffer.append(line)
                logging.debug(f"Gatttool interactive connection output: {line.strip()}")
                
                # Check for connection success message
                if "Connection successful" in line:
                    connection_success_reported = True
                
                # Check for the prompt. The prompt often appears on the same line as the 'connect' command output.
                if re.search(r"\[LE\]>", line): 
                    prompt_received = True

                # If both conditions are met, we are successfully connected and ready
                if connection_success_reported and prompt_received:
                    logging.info("Interactive gatttool connection established and prompt received.")
                    return True

                if time.time() - start_time > GATTTOOL_TIMEOUT:
                    logging.error(f"Timed out waiting for gatttool interactive prompt/connection on attempt {attempt + 1}. Output: {''.join(output_buffer)}")
                    break # Break to retry

            # If loop finished without returning True, connection failed for this attempt
            if gatttool_proc.poll() is None: # If process is still running, try to terminate
                gatttool_proc.terminate()
                gatttool_proc.wait(timeout=1)

        except Exception as e:
            logging.error(f"Error starting gatttool in interactive mode on attempt {attempt + 1}: {e}")
        
        time.sleep(RETRY_DELAY) # Wait before retrying connection
    
    logging.error("Failed to establish interactive gatttool connection after multiple attempts.")
    return False

def send_gatttool_interactive_command(command_hex: str, retries: int = 3) -> bool:
    """Sends a command via the persistent gatttool interactive session."""
    global gatttool_proc, last_command_time

    if not CHAR_HANDLE:
        logging.error("Characteristic handle not discovered. Cannot send interactive command.")
        return False
    
    # CRITICAL CHANGE: Always try to connect before sending a command.
    # This ensures we have a fresh connection for each command.
    if not connect_gatttool_interactive():
        logging.error("Failed to establish connection before sending command. Cannot send command.")
        return False

    current_time = time.time()
    if current_time - last_command_time < COMMAND_DELAY:
        time.sleep(COMMAND_DELAY - (current_time - last_command_time))

    with connection_lock: # Still use lock for thread safety
        for attempt in range(retries):
            try:
                # Command to write: char-write-cmd <handle> <value>
                cmd_to_send = f"char-write-cmd {CHAR_HANDLE} {command_hex}\n"
                
                logging.debug(f"Sending interactive command: {cmd_to_send.strip()}")
                gatttool_proc.stdin.write(cmd_to_send)
                gatttool_proc.stdin.flush() # Ensure command is sent immediately

                # Read output until a new prompt or timeout
                output_buffer = []
                start_time = time.time()
                
                # We expect the echo and prompt on the same line, but must prioritize "Disconnected"
                while True: 
                    line = gatttool_proc.stdout.readline()
                    if not line: # EOF, process terminated
                        logging.error(f"Gatttool process terminated unexpectedly after sending command. No response received for: {cmd_to_send.strip()}")
                        return False # Immediate failure if process dies
                    
                    output_buffer.append(line)
                    logging.debug(f"Gatttool response: {line.strip()}")
                    
                    # CRITICAL: If "Disconnected" is seen, this command failed. Return False immediately.
                    if "Command Failed: Disconnected" in line:
                        logging.warning(f"Gatttool reported 'Command Failed: Disconnected' for command {command_hex}. This command failed.")
                        # Do NOT try to reconnect here; the outer loop will handle it by calling connect_gatttool_interactive again
                        return False 
                    
                    # If we get here, it means "Disconnected" was NOT in the line.
                    # Now check for successful echo and prompt.
                    if re.search(r"\[LE\]>", line) and (command_hex in line or f"char-write-cmd {CHAR_HANDLE}" in line):
                        logging.info(f"Interactive command sent successfully (echoed and prompt returned): {command_hex}")
                        last_command_time = time.time()
                        return True
                    
                    if time.time() - start_time > GATTTOOL_TIMEOUT:
                        logging.warning(f"Interactive gatttool command timed out on attempt {attempt + 1}. Expected prompt or echo not seen. Last line: {line.strip() if line else 'None'}")
                        break # Break to retry (will fall through to outer retry logic)

            except Exception as e:
                logging.error(f"Error sending interactive command on attempt {attempt + 1}: {e}")
            
            # If command failed or timed out, and it's not the last attempt, try again.
            # The reconnection logic is now handled by the initial connect_gatttool_interactive() call in this function.
            if attempt < retries - 1:
                logging.info(f"Retrying command {command_hex} (attempt {attempt + 2}).")
                time.sleep(RETRY_DELAY)
        
        logging.error(f"Failed to send interactive command after {retries} attempts")
        return False


# Replace send_gatttool_command with the interactive version
send_gatttool_command = send_gatttool_interactive_command


def test_device_connection() -> bool:
    # This function is now less critical as interactive connection handles it
    # but keeping it for initial check.
    logging.info("Testing device connection with gatttool (non-interactive check)...")
    try:
        cmd = ['gatttool', '-b', DEVICE_MAC, '-t', DEVICE_ADDR_TYPE, '--primary']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=GATTTOOL_TIMEOUT)
        
        if result.returncode == 0:
            logging.info("Device connection test successful with gatttool.")
            return True
        else:
            logging.error(f"Device connection test failed with gatttool: {result.stderr.strip()}")
            return False
            
    except subprocess.TimeoutExpired:
        logging.error(f"Device connection test timed out after {GATTTOOL_TIMEOUT} seconds.")
        return None
    except Exception as e:
        logging.error(f"Connection test error: {e}")
        return False

def send_keepalive():
    """
    Sends a keepalive command to the device.
    If the light is ON, it sends the current color/brightness command to refresh.
    If the light is OFF, it sends a generic keepalive.
    """
    global last_keepalive_time
    current_time = time.time()
    
    if current_time - last_keepalive_time > KEEPALIVE_INTERVAL:
        logging.info("Sending keepalive command.")
        if current_state == "ON":
            cmd = get_ble_color_command(current_r, current_g, current_b, current_brightness_ha)
            # Use 1 retry for keepalive, and allow it to fail if reconnection doesn't work
            if send_gatttool_command(cmd, retries=1): 
                last_keepalive_time = current_time
            else:
                logging.warning("Keepalive command failed. Connection might be lost.")
        else:
            # For OFF state, sending the KEEPALIVE_COMMAND_HEX is often used to query status
            # or prevent deep sleep without turning the light on.
            if send_gatttool_command(KEEPALIVE_COMMAND_HEX, retries=1):
                last_keepalive_time = current_time
            else:
                logging.warning("Keepalive command failed for OFF state. Connection might be lost.")

def on_connect(client_instance, userdata, flags, rc, properties):
    """MQTT connection callback."""
    if rc == 0:
        logging.info("Connected to MQTT broker successfully")
        # CRITICAL CHANGE: Only subscribe to the single command topic
        client_instance.subscribe(f"{BASE_TOPIC}/light/set") 
        publish_state() # Publish initial state on connect
    else:
        logging.error(f"Failed to connect to MQTT broker with code {rc}")

def on_message(client_instance, userdata, msg):
    """Handle incoming MQTT messages (now expects JSON payload)."""
    global current_state, current_brightness_ha, current_r, current_g, current_b
    
    topic = msg.topic
    payload = msg.payload.decode('utf-8')
    
    logging.info(f"Received MQTT message: {topic} = {payload}")
    
    try:
        command_data = json.loads(payload)
        logging.debug(f"on_message: Parsed command_data: {command_data}")

        # Handle state (ON/OFF)
        if "state" in command_data:
            new_state = command_data["state"].upper()
            logging.debug(f"on_message: new_state from payload = {new_state}")
            if new_state == "ON":
                current_state = "ON"
            elif new_state == "OFF":
                current_state = "OFF"
            logging.debug(f"on_message: current_state after update logic = {current_state}")
                
        # Handle brightness
        if "brightness" in command_data:
            current_brightness_ha = max(0, min(255, command_data["brightness"]))
            logging.debug(f"on_message: current_brightness_ha after update logic = {current_brightness_ha}")
        
        # Handle color
        if "color" in command_data and isinstance(command_data["color"], dict):
            color_data = command_data["color"]
            current_r = max(0, min(255, color_data.get('r', current_r)))
            current_g = max(0, min(255, color_data.get('g', current_g)))
            current_b = max(0, min(255, color_data.get('b', current_b)))
            logging.debug(f"on_message: current_r={current_r}, current_g={current_g}, current_b={current_b} after update logic")
        
        # Send the BLE command based on the updated state
        if current_state == "ON":
            cmd = get_ble_color_command(current_r, current_g, current_b, current_brightness_ha)
            logging.debug(f"on_message: Calculated BLE command: {cmd} for R:{current_r}, G:{current_g}, B:{current_b}, Brightness:{current_brightness_ha}")
            send_gatttool_command(cmd)
        elif current_state == "OFF":
            logging.debug(f"on_message: Sending OFF command: {OFF_COMMAND_BYTES_HEX}")
            send_gatttool_command(OFF_COMMAND_BYTES_HEX)
            
        logging.debug(f"on_message: State before publishing: current_state={current_state}, current_brightness_ha={current_brightness_ha}, current_r={current_r}, current_g={current_g}, current_b={current_b}")
        publish_state() # Publish state after change
                    
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON payload: {e} - Payload: {payload}")
    except Exception as e:
        logging.error(f"Error processing MQTT message: {e}")

def publish_state():
    """Publish current state to Home Assistant."""
    state_payload = {
        "state": current_state,
        "brightness": current_brightness_ha,
        "color": {
            "r": current_r,
            "g": current_g,
            "b": current_b
        }
    }
    
    if client and client.is_connected():
        client.publish(f"{BASE_TOPIC}/light/state", json.dumps(state_payload), retain=True)
        logging.info(f"Published state: {state_payload}")
    else:
        logging.warning("MQTT client not connected, unable to publish state")

def signal_handler(signum, frame):
    """Handle graceful shutdown."""
    logging.info("Shutting down gracefully...")
    if gatttool_proc:
        logging.info("Terminating gatttool interactive process.")
        try:
            gatttool_proc.stdin.write("exit\n")
            gatttool_proc.stdin.flush()
            gatttool_proc.terminate()
            gatttool_proc.wait(timeout=2) # Give it a moment to exit
        except Exception as e:
            logging.error(f"Error during gatttool process termination: {e}")
    if client:
        client.disconnect()
    sys.exit(0)

def main():
    """Main program execution."""
    global client, last_command_time
    last_command_time = time.time()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Discover characteristic handle first (still needed for the handle)
    if not discover_characteristic_handle():
        logging.critical("Failed to discover characteristic handle. Cannot proceed.")
        return
        
    # Establish persistent gatttool connection
    if not connect_gatttool_interactive():
        logging.critical("Failed to establish persistent gatttool connection. Cannot proceed.")
        return
    
    # *** NEW: Send POWER_ON command immediately after connection ***
    logging.info("Sending initial POWER_ON command to ensure light is on.")
    # Set initial state to ON and publish it
    global current_state
    current_state = "ON" 
    publish_state() # Publish the ON state immediately

    if not send_gatttool_command(POWER_ON_COMMAND_HEX, retries=3):
        logging.error("Failed to send initial POWER_ON command. Light might not be responsive.")
        # If the initial power on fails, revert state to OFF
        current_state = "OFF"
        publish_state()
    
    # Set up MQTT
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USERNAME and MQTT_PASSWORD:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    
    client.on_connect = on_connect
    client.on_message = on_message
    
    client_id = f"bt_mqtt_bedframe_gatttool_{random.randint(0, 10000)}"
    client._client_id = client_id.encode('utf-8')
    logging.info(f"MQTT Client ID: {client_id}")
    
    try:
        client.connect(MQTT_BROKER_IP, MQTT_PORT, MQTT_KEEP_ALIVE)
        
        # Start a separate thread for keepalives
        keepalive_thread = threading.Thread(target=schedule_keepalive, args=(KEEPALIVE_INTERVAL,))
        keepalive_thread.daemon = True # Allow main program to exit even if this thread is running
        keepalive_thread.start()
        
        client.loop_forever() # Blocks and handles MQTT messages
    except Exception as e:
        logging.error(f"MQTT or main loop error: {e}")
    finally:
        if client:
            client.disconnect()
        # Ensure gatttool process is terminated on exit
        if gatttool_proc and gatttool_proc.poll() is None:
            logging.info("Terminating gatttool interactive process on main exit.")
            try:
                gatttool_proc.stdin.write("exit\n")
                gatttool_proc.stdin.flush()
                gatttool_proc.terminate()
                gatttool_proc.wait(timeout=2)
            except Exception as e:
                logging.error(f"Error during gatttool process termination: {e}")

def schedule_keepalive(interval):
    """Periodically sends a keepalive command."""
    global last_keepalive_time
    while True:
        send_keepalive()
        time.sleep(interval)

if __name__ == "__main__":
    main()
