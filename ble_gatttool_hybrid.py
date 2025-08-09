#!/usr/bin/env python
"""
Production-grade BLE LED Controller using bleak and aiomqtt.
Features: Availability reporting, state reconciliation, and exponential backoff.
This version uses an aggressive "wake-up" command sequence upon reconnect
to force the controller out of its deep sleep state.
"""

import asyncio
import json
import logging
import signal
import yaml

import aiomqtt
from bleak import BleakClient
from bleak.exc import BleakError

# --- Configuration (loaded from secrets.yaml) ---
CONFIG = {}

# --- Constants ---
CHAR_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"
BASE_RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60

# --- Global State ---
current_light_state = {"state": "ON", "brightness": 255, "color": {"r": 255, "g": 255, "b": 255}}
shutdown_event = asyncio.Event()
disconnected_event = asyncio.Event()

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- Core Functions ---

async def send_ble_command(client: BleakClient, hex_command: str):
    try:
        if client.is_connected:
            command_bytes = bytes.fromhex(hex_command)
            await client.write_gatt_char(CHAR_UUID, command_bytes, response=False)
            logging.info(f"Sent command: {hex_command}")
            await asyncio.sleep(0.1)
        else:
            logging.warning("Client disconnected, command not sent.")
    except BleakError as e:
        logging.error(f"Failed to send command {hex_command}: {e}")

async def send_update_to_light(ble_client: BleakClient, is_wakeup_call=False):
    """Sends the current color and brightness commands to the light."""
    if current_light_state["state"] == "ON":
        # On a fresh reconnect, send an explicit ON command first.
        if is_wakeup_call:
            await send_ble_command(ble_client, "7e0404010000ff00ef")

        color = current_light_state["color"]
        r, g, b = int(color["r"]), int(color["g"]), int(color["b"])
        color_cmd = f"7e070503{r:02x}{g:02x}{b:02x}00ef"
        await send_ble_command(ble_client, color_cmd)

        brightness_ha = current_light_state["brightness"]
        brightness_ble = max(1, min(100, int((brightness_ha / 255.0) * 100)))
        brightness_cmd = f"7e0501{brightness_ble:02x}000000ef"
        await send_ble_command(ble_client, brightness_cmd)
    else:
        await send_ble_command(ble_client, "7e0404000000ff00ef")

async def reconcile_state(ble_client: BleakClient, mqtt_client: aiomqtt.Client):
    """Makes the physical light match the desired state upon connection."""
    logging.info(f"Reconciling state to: {current_light_state}")
    # ✅ THE AGGRESSIVE WAKE-UP CALL
    await send_update_to_light(ble_client, is_wakeup_call=True)
    
    await mqtt_client.publish(
        f"{CONFIG['base_topic']}/state",
        payload=json.dumps(current_light_state),
        retain=True,
    )

async def handle_mqtt_message(ble_client: BleakClient, payload: str, mqtt_client: aiomqtt.Client):
    """Updates the desired state and sends the command if connected."""
    global current_light_state
    try:
        data = json.loads(payload)
        current_light_state.update(data)
        logging.info(f"Desired state updated to: {current_light_state}")
        
        await mqtt_client.publish(
            f"{CONFIG['base_topic']}/state",
            payload=json.dumps(current_light_state),
            retain=True,
        )

        if ble_client.is_connected:
            await send_update_to_light(ble_client)
        
    except Exception as e:
        logging.error(f"Error processing MQTT message: {e}")

def on_disconnect(client: BleakClient) -> None:
    logging.warning(f"Device disconnected! Setting event to trigger reconnection.")
    disconnected_event.set()

async def listen_for_mqtt(ble_client: BleakClient, mqtt_client: aiomqtt.Client):
    """A dedicated task to handle incoming MQTT messages."""
    await mqtt_client.subscribe(f"{CONFIG['base_topic']}/set")
    async for message in mqtt_client.messages:
        await handle_mqtt_message(ble_client, message.payload.decode(), mqtt_client)

async def main():
    global CONFIG
    try:
        with open('secrets.yaml', 'r') as f:
            CONFIG = yaml.safe_load(f)
            CONFIG.setdefault('base_topic', 'bedframe/light')
            CONFIG.setdefault('mqtt_port', 1883)
    except FileNotFoundError:
        logging.critical("CRITICAL: secrets.yaml not found.")
        return

    retry_delay = BASE_RECONNECT_DELAY
    while not shutdown_event.is_set():
        try:
            async with BleakClient(
                CONFIG['device_mac'],
                timeout=20.0,
                disconnected_callback=on_disconnect
            ) as ble_client:
                logging.info(f"Successfully connected to BLE device: {CONFIG['device_mac']}")
                retry_delay = BASE_RECONNECT_DELAY
                
                async with aiomqtt.Client(
                    hostname=CONFIG['mqtt_broker'],
                    port=CONFIG['mqtt_port'],
                    username=CONFIG.get('mqtt_username'),
                    password=CONFIG.get('mqtt_password'),
                    will=aiomqtt.Will(topic=f"{CONFIG['base_topic']}/availability", payload="offline", retain=True)
                ) as mqtt_client:
                    
                    await mqtt_client.publish(f"{CONFIG['base_topic']}/availability", "online", retain=True)
                    logging.info("Successfully connected to MQTT broker.")
                    
                    await reconcile_state(ble_client, mqtt_client)

                    mqtt_listener_task = asyncio.create_task(listen_for_mqtt(ble_client, mqtt_client))
                    disconnected_event.clear()
                    shutdown_task = asyncio.create_task(shutdown_event.wait())
                    disconnected_task = asyncio.create_task(disconnected_event.wait())

                    await asyncio.wait([disconnected_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED)
                    mqtt_listener_task.cancel()

        except Exception as e:
            logging.error(f"Main loop error: {e}")
            try:
                async with aiomqtt.Client(hostname=CONFIG['mqtt_broker'], port=CONFIG['mqtt_port'], username=CONFIG.get('mqtt_username'), password=CONFIG.get('mqtt_password')) as mqtt_client:
                    await mqtt_client.publish(f"{CONFIG['base_topic']}/availability", "offline", retain=True)
            except Exception as mqtt_e:
                logging.error(f"Could not publish offline status: {mqtt_e}")

            logging.info(f"Retrying in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(MAX_RECONNECT_DELAY, retry_delay * 2)

def shutdown_handler(sig, frame):
    logging.info("Initiating graceful shutdown...")
    shutdown_event.set()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.info("Application has shut down.")
import asyncio
import json
import logging
import signal
import yaml

import aiomqtt
from bleak import BleakClient
from bleak.exc import BleakError

# --- Configuration (loaded from secrets.yaml) ---
CONFIG = {}

# --- Constants ---
CHAR_UUID = "0000fff3-0000-1000-8000-00805f9b34fb"
BASE_RECONNECT_DELAY = 5
MAX_RECONNECT_DELAY = 60

# --- Global State ---
current_light_state = {"state": "ON", "brightness": 255, "color": {"r": 255, "g": 255, "b": 255}}
shutdown_event = asyncio.Event()
disconnected_event = asyncio.Event()

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- Core Functions ---

async def send_ble_command(client: BleakClient, hex_command: str):
    try:
        if client.is_connected:
            command_bytes = bytes.fromhex(hex_command)
            await client.write_gatt_char(CHAR_UUID, command_bytes, response=False)
            logging.info(f"Sent command: {hex_command}")
            await asyncio.sleep(0.1)
        else:
            logging.warning("Client disconnected, command not sent.")
    except BleakError as e:
        logging.error(f"Failed to send command {hex_command}: {e}")

async def send_update_to_light(ble_client: BleakClient, is_wakeup_call=False):
    """Sends the current color and brightness commands to the light."""
    if current_light_state["state"] == "ON":
        if is_wakeup_call:
            await send_ble_command(ble_client, "7e0404010000ff00ef")

        color = current_light_state["color"]
        r, g, b = int(color["r"]), int(color["g"]), int(color["b"])
        color_cmd = f"7e070503{r:02x}{g:02x}{b:02x}00ef"
        await send_ble_command(ble_client, color_cmd)

        brightness_ha = current_light_state["brightness"]
        brightness_ble = max(1, min(100, int((brightness_ha / 255.0) * 100)))
        brightness_cmd = f"7e0501{brightness_ble:02x}000000ef"
        await send_ble_command(ble_client, brightness_cmd)
    else:
        await send_ble_command(ble_client, "7e0404000000ff00ef")

async def reconcile_state(ble_client: BleakClient, mqtt_client: aiomqtt.Client):
    """Makes the physical light match the desired state upon connection."""
    logging.info(f"Reconciling state to: {current_light_state}")
    await send_update_to_light(ble_client, is_wakeup_call=True)
    
    await mqtt_client.publish(
        f"{CONFIG['base_topic']}/state",
        payload=json.dumps(current_light_state),
        retain=True,
    )

async def handle_mqtt_message(ble_client: BleakClient, payload: str, mqtt_client: aiomqtt.Client):
    """Updates the desired state and sends the command if connected."""
    global current_light_state
    try:
        data = json.loads(payload)
        
        is_turning_on_from_off = (
            "state" in data and 
            data["state"].upper() == "ON" and 
            current_light_state["state"] == "OFF"
        )

        current_light_state.update(data)
        logging.info(f"Desired state updated to: {current_light_state}")
        
        await mqtt_client.publish(
            f"{CONFIG['base_topic']}/state",
            payload=json.dumps(current_light_state),
            retain=True,
        )

        if ble_client.is_connected:
            if is_turning_on_from_off:
                logging.info("WAKE-UP CALL: Forcing a reconnect to turn ON from OFF state.")
                disconnected_event.set()
            else:
                await send_update_to_light(ble_client)
        
    except Exception as e:
        logging.error(f"Error processing MQTT message: {e}")

def on_disconnect(client: BleakClient) -> None:
    logging.warning(f"Device disconnected! Setting event to trigger reconnection.")
    disconnected_event.set()

async def listen_for_mqtt(ble_client: BleakClient, mqtt_client: aiomqtt.Client):
    """A dedicated task to handle incoming MQTT messages."""
    await mqtt_client.subscribe(f"{CONFIG['base_topic']}/set")
    async for message in mqtt_client.messages:
        await handle_mqtt_message(ble_client, message.payload.decode(), mqtt_client)

async def main():
    global CONFIG
    try:
        with open('secrets.yaml', 'r') as f:
            CONFIG = yaml.safe_load(f)
            CONFIG.setdefault('base_topic', 'bedframe/light')
            CONFIG.setdefault('mqtt_port', 1883)
    except FileNotFoundError:
        logging.critical("CRITICAL: secrets.yaml not found.")
        return

    retry_delay = BASE_RECONNECT_DELAY
    while not shutdown_event.is_set():
        try:
            async with BleakClient(
                CONFIG['device_mac'],
                timeout=20.0,
                disconnected_callback=on_disconnect
            ) as ble_client:
                logging.info(f"Successfully connected to BLE device: {CONFIG['device_mac']}")
                retry_delay = BASE_RECONNECT_DELAY
                
                async with aiomqtt.Client(
                    hostname=CONFIG['mqtt_broker'],
                    port=CONFIG['mqtt_port'],
                    username=CONFIG.get('mqtt_username'),
                    password=CONFIG.get('mqtt_password'),
                    will=aiomqtt.Will(topic=f"{CONFIG['base_topic']}/availability", payload="offline", retain=True)
                ) as mqtt_client:
                    
                    await mqtt_client.publish(f"{CONFIG['base_topic']}/availability", "online", retain=True)
                    logging.info("Successfully connected to MQTT broker.")
                    
                    await reconcile_state(ble_client, mqtt_client)

                    mqtt_listener_task = asyncio.create_task(listen_for_mqtt(ble_client, mqtt_client))
                    disconnected_event.clear()
                    shutdown_task = asyncio.create_task(shutdown_event.wait())
                    disconnected_task = asyncio.create_task(disconnected_event.wait())

                    await asyncio.wait([disconnected_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED)
                    mqtt_listener_task.cancel()

        except Exception as e:
            logging.error(f"Main loop error: {e}")
            try:
                async with aiomqtt.Client(hostname=CONFIG['mqtt_broker'], port=CONFIG['mqtt_port'], username=CONFIG.get('mqtt_username'), password=CONFIG.get('mqtt_password')) as mqtt_client:
                    await mqtt_client.publish(f"{CONFIG['base_topic']}/availability", "offline", retain=True)
            except Exception as mqtt_e:
                logging.error(f"Could not publish offline status: {mqtt_e}")

            logging.info(f"Retrying in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(MAX_RECONNECT_DELAY, retry_delay * 2)

def shutdown_handler(sig, frame):
    logging.info("Initiating graceful shutdown...")
    shutdown_event.set()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.info("Application has shut down.")
