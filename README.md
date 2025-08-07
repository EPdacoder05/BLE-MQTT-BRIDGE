### Device Discovery

To control a new device, you first need its **MAC address** and the correct **handle** for the command characteristic.

1.  **Find the MAC Address:** Use a Bluetooth scanning tool to find your device. On most Linux systems, you can use `bluetoothctl`:
    ```bash
    # Start the tool
    bluetoothctl
    # Start scanning
    scan on
    # Look for your device in the list and copy its MAC address
    ```

2.  **Find the Characteristic Handle:** Run the following command, replacing `<YOUR_DEVICE_MAC>` with the address you found.
    ```bash
    sudo gatttool -t public -b <YOUR_DEVICE_MAC> --char-desc
    ```
    Look for the line containing the UUID `0000fff3-0000-1000-8000-00805f9b34fb`. The `handle` value on that line is what you need to set in the script.
