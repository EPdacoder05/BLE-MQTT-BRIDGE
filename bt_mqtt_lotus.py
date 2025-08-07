import paho.mqtt.client as mqtt
from bluepy.btle import Peripheral, DefaultDelegate
import time
import binascii
import json

# --- Configuration ---
# Your Device's MAC Address (Confirmed)
DEVICE_MAC_ADDRESS = "BE:67:00:5B:04:4A"

# GATT Characteristic UUID for write operations (Common for ELK-BLEDOM)
CHARACTERISTIC_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"

# GATT Characteristic Handle (Confirmed from your logs)
WRITABLE_CHAR_HANDLE = 0x0009

# MQTT Broker Details (Confirmed)
MQTT_BROKER_IP = "192.168.12.124"
MQTT_PORT = 1883
MQTT_USERNAME = None  # Set if your MQTT broker requires authentication
MQTT_PASSWORD = None  # Set if your MQTT broker requires authentication

# MQTT Topics (MUST match Home Assistant config for JSON schema)
MQTT_COMMAND_TOPIC_JSON = "bedframe/light/set_json"
MQTT_STATE_TOPIC_JSON = "bedframe/light/state_json" # Optional, for state feedback from device

# --- Discovered Commands (Confirmed from your analysis) ---
# Lights OFF command (Type 2)
COMMAND_OFF = bytes.fromhex("7e0404000000ff00ef")

# Color Command Structure (Type 1): 7e070503 RR GG BB BR ef
# Header (fixed part before RGB)
COLOR_COMMAND_HEADER = bytes.fromhex("7e070503")
# Footer (fixed part after Brightness byte)
COLOR_COMMAND_FOOTER = bytes.fromhex("ef") # 'ef' is directly after the brightness byte

# Global state variables to track current color and brightness
# Initialize to a default state (e.g., Pure White at Max Brightness)
current_r = 255
current_g = 255
current_b = 255
current_brightness_ha = 255 # Stores HA's 0-255 brightness value

# --- Brightness Mapping Function ---
def map_ha_brightness_to_device(ha_brightness: int) -> int:
    """
    Maps Home Assistant's 0-255 brightness to the device's 16-255 range.
    Handles 0 brightness specifically to ensure the device sends its minimum.
    """
    if ha_brightness == 0:
        return 16 # Smallest observed brightness
    
    # Linear mapping from HA's 0-255 to device's 16-255 range
    # Device range: 255 - 16 = 239
    # Scale HA brightness (0-255) to 0-239 and then add 16
    device_brightness = int(16 + (ha_brightness / 255) * (255 - 16))
    
    # Ensure it stays within valid bounds (16-255)
    return max(16, min(255, device_brightness))

# Bluetooth Peripheral object
periph = None
characteristic = None

class MyDelegate(DefaultDelegate):
    def __init__(self):
        DefaultDelegate.__init__(self)

    def handleNotification(self, cHandle, data):
        # This function processes notifications (data sent from the BLE device to us).
        # Most simple LED strips don't send state back, but if yours does,
        # you can parse 'data' and publish it to MQTT_STATE_TOPIC_JSON.
        print(f"Notification from handle {cHandle}: {binascii.hexlify(data).decode('utf-8')}")

def connect_to_device():
    global periph, characteristic
    print(f"Attempting to connect to {DEVICE_MAC_ADDRESS}...")
    try:
        # Establish connection to the BLE peripheral
        periph = Peripheral(DEVICE_MAC_ADDRESS)
        periph.setDelegate(MyDelegate()) # Set delegate to handle notifications
        print("Connected.")

        # Get the specific characteristic using the UUID and verify its handle
        char_list = periph.getCharacteristics(uuid=CHARACTERISTIC_UUID)
        if not char_list:
            raise Exception(f"Characteristic {CHARACTERISTIC_UUID} not found.")
        
        characteristic = char_list[0]
        print(f"Found characteristic {CHARACTERISTIC_UUID} at handle {hex(characteristic.handle)}")

        if characteristic.handle != WRITABLE_CHAR_HANDLE:
            print(f"WARNING: Characteristic handle mismatch! Sniffed: {hex(WRITABLE_CHAR_HANDLE)}, Found: {hex(characteristic.handle)}")
            print("Please ensure WRITABLE_CHAR_HANDLE in script matches your Wireshark analysis.")

        return True
    except Exception as e:
        print(f"Failed to connect or find characteristic: {e}")
        periph = None
        characteristic = None
        return False

def send_command(command_bytes):
    if characteristic and periph and periph.connected():
        try:
            # Write the command bytes to the characteristic.
            # Most LED controllers use 'write without response' (withResponse=False)
            characteristic.write(command_bytes, withResponse=False)
            print(f"Sent: {binascii.hexlify(command_bytes).decode('utf-8')}")
            return True
        except Exception as e:
            print(f"Error sending command: {e}")
            return False
    else:
        print("Not connected to device or characteristic not found.")
        return False

# --- MQTT Callbacks ---
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to MQTT Broker!")
        # Subscribe to the JSON command topic from Home Assistant
        client.subscribe(MQTT_COMMAND_TOPIC_JSON)
    else:
        print(f"Failed to connect to MQTT, return code {rc}\n")

def on_message(client, userdata, msg):
    global current_r, current_g, current_b, current_brightness_ha
    topic = msg.topic
    payload = msg.payload.decode()
    print(f"MQTT Message Received - Topic: {topic}, Payload: {payload}")

    if topic == MQTT_COMMAND_TOPIC_JSON:
        try:
            data = json.loads(payload)

            # Handle ON/OFF state from Home Assistant
            if "state" in data:
                if data["state"].upper() == "OFF":
                    send_command(COMMAND_OFF)
                    current_brightness_ha = 0 # Update internal state
                    print("Lights turned OFF.")
                    return # Exit early after sending the OFF command

                elif data["state"].upper() == "ON":
                    # As no separate ON command was identified, send the last known color/brightness.
                    # If brightness was 0 (from being off), send a default minimum.
                    device_brightness = map_ha_brightness_to_device(max(1, current_brightness_ha))
                    command_bytes = COLOR_COMMAND_HEADER + bytes([current_r, current_g, current_b, device_brightness]) + COLOR_COMMAND_FOOTER
                    send_command(command_bytes)
                    print(f"Lights turned ON to last known color (RGB: {current_r},{current_g},{current_b}, Brightness: {current_brightness_ha}).")
                    return # Exit early after sending the ON command

            # Update RGB values if present in the JSON payload
            if "color" in data and "r" in data["color"] and "g" in data["color"] and "b" in data["color"]:
                current_r = data["color"]["r"]
                current_g = data["color"]["g"]
                current_b = data["color"]["b"]

            # Update brightness value if present in the JSON payload
            if "brightness" in data:
                current_brightness_ha = max(0, min(255, data["brightness"]))

            # Convert HA brightness (0-255) to device brightness (16-255)
            device_brightness = map_ha_brightness_to_device(current_brightness_ha)

            # Construct and send the combined color/brightness command
            command_bytes = COLOR_COMMAND_HEADER + bytes([current_r, current_g, current_b, device_brightness]) + COLOR_COMMAND_FOOTER
            send_command(command_bytes)

            # Optional: Publish state back to Home Assistant for visual feedback
            # Uncomment this section if you set `optimistic: false` in your HA config
            # and your device *actually* sends back state notifications that you can parse
            # state_payload = {
            #     "state": "ON" if current_brightness_ha > 0 else "OFF",
            #     "color": {"r": current_r, "g": current_g, "b": current_b},
            #     "brightness": current_brightness_ha
            # }
            # client.publish(MQTT_STATE_TOPIC_JSON, json.dumps(state_payload))

        except json.JSONDecodeError:
            print(f"Invalid JSON payload received on {MQTT_COMMAND_TOPIC_JSON}: {payload}")
        except Exception as e:
            print(f"Error processing MQTT message: {e}")

# --- Main Program Execution ---
if __name__ == "__main__":
    client = mqtt.Client()
    if MQTT_USERNAME and MQTT_PASSWORD:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message

    # Connect to the MQTT broker
    try:
        client.connect(MQTT_BROKER_IP, MQTT_PORT, 60)
        client.loop_start() # Start MQTT loop in a separate thread
    except Exception as e:
        print(f"Failed to connect to MQTT broker: {e}")
        exit(1) # Exit if MQTT connection fails, as the bridge won't function without it

    # Main loop for Bluetooth connection management
    while True:
        # Check if Bluetooth peripheral is connected
        if periph is None or not periph.connected():
            if connect_to_device():
                print("Bluetooth device reconnected.")
                # After successful reconnect, send the last known state
                # This helps synchronize the physical device if it was offline.
                device_brightness = map_ha_brightness_to_device(current_brightness_ha)
                command_bytes = COLOR_COMMAND_HEADER + bytes([current_r, current_g, current_b, device_brightness]) + COLOR_COMMAND_FOOTER
                send_command(command_bytes)
            else:
                print("Retrying Bluetooth connection in 10 seconds...")
                time.sleep(10) # Wait before retrying connection
        else:
            try:
                # Keep the connection alive and check for notifications (non-blocking)
                periph.waitForNotifications(1.0)
            except Exception as e:
                print(f"Bluetooth communication error: {e}. Attempting to reconnect.")
                # Clean up old connection before retrying
                if periph:
                    try:
                        periph.disconnect()
                    except:
                        pass # Ignore errors during disconnect
                periph = None
                characteristic = None
                time.sleep(5) # Wait before attempting reconnect

        time.sleep(0.1) # Small delay to prevent busy-looping
