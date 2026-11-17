#!/usr/bin/env python3

"""
EECS113 Final Project: Smart Home Controller

Hardware used:
- LCD display (I2C)
- ADC module (I2C)
- Thermistor
- Photoresistor 
- Ultrasonic sensor
- Button for mode switching
- LEDs for mode/activity indication

The script supports two modes:
- Normal Mode: read temperature, show time, display mode, LED indicator
- Security Mode: read light and distance, detect suspicious events, blink LED,
  log events, and send an email alert
"""

import datetime
import math
import random
import smtplib
import ssl
import sys
import threading
import time


# import necessary packages rpi.gpio, spidev, smbus2, and rplcd
try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except (ImportError, RuntimeError):
    HAS_GPIO = False

try:
    import spidev
    HAS_SPI = True
except ImportError:
    HAS_SPI = False

try:
    import smbus2 as smbus
    HAS_SMBUS = True
except ImportError:
    try:
        import smbus
        HAS_SMBUS = True
    except ImportError:
        HAS_SMBUS = False

try:
    from RPLCD.i2c import CharLCD
    HAS_LCD = True
except ImportError:
    HAS_LCD = False

# configuration
NORMAL_MODE = "NORMAL"
SECURITY_MODE = "SECURITY"

# GPIO pin configuration (BCM numbering)
BUTTON_PIN = 17           # physical pin 11 
NORMAL_LED_PIN = 27       # physical pin 13
SECURITY_LED_PIN = 22     # physical pin 15
TRIG_PIN = 23             # physical pin 16
ECHO_PIN = 24             # physical pin 18

# ADC channel assignments
TEMP_CHANNEL = 0    # A0
LIGHT_CHANNEL = 2   # A2

# sensor thresholds and behavior
LIGHT_CHANGE_THRESHOLD = 120
PROXIMITY_THRESHOLD_CM = 40
ALERT_COOLDOWN_SECONDS = 30

# thermistor calibration constants
REFERENCE_RESISTOR = 10000.0
BETA_VALUE = 3950.0
NOMINAL_TEMPERATURE_C = 25.0
NOMINAL_RESISTANCE = 10000.0
V_REF = 3.3
ADC_MAX = 1023

# email notification configuration
EMAIL_SETTINGS = {
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 465,
    "sender_email": "your.email@gmail.com",
    "receiver_email": "recipient@example.com",
    "password": "your_app_password",
}

# LCD configuration for I2C 1602 displays
LCD_I2C_ADDRESS = 0x27
LCD_COLUMNS = 16
LCD_ROWS = 2

# I2C ADC configuration
I2C_ADC_MODEL = "ADS7830"
I2C_ADC_ADDRESS = 0x4b
I2C_BUS = 1
ADS7830_COMMAND_BASE = 0x84

# timing
LOOP_DELAY_SECONDS = 1.0

# LCD manager functions (initialize, clear, write)
class LCDManager:
    # initialize LCD
    def __init__(self):
        self.lcd = None
        if HAS_LCD:
            try:
                self.lcd = CharLCD(
                    i2c_expander="PCF8574",
                    address=LCD_I2C_ADDRESS,
                    port=1,
                    cols=LCD_COLUMNS,
                    rows=LCD_ROWS,
                    charmap="A00",
                )
            except Exception:
                self.lcd = None

    # clear LCD
    def clear(self):
        if self.lcd:
            try:
                self.lcd.clear()
            except Exception:
                self.lcd = None

    
    # write to LCD
    def write(self, line1: str, line2: str = ""):
        if self.lcd:
            try:
                self.lcd.clear()
                self.lcd.write_string(line1.ljust(LCD_COLUMNS)[:LCD_COLUMNS])
                if LCD_ROWS > 1:
                    self.lcd.crlf()
                    self.lcd.write_string(line2.ljust(LCD_COLUMNS)[:LCD_COLUMNS])
                return
            except Exception:
                self.lcd = None

        # Fallback output when LCD is unavailable
        # print("=" * 40)
        # print(line1)
        # if line2:
        #     print(line2)
        # print("=" * 40)

# functions for reading from ADC module
class ADCReader:
    def __init__(self):
        self.spi = None
        self.i2c = None
        self.i2c_ready = False

        # prefer I2C for known I2C ADC model (ADS7830)
        if HAS_SMBUS and I2C_ADC_MODEL == "ADS7830":
            try:
                self.i2c = smbus.SMBus(I2C_BUS)
                self.i2c_ready = self._probe_i2c_adc()
                if self.i2c_ready:
                    print(f"[ADC] Detected {I2C_ADC_MODEL} at address {hex(I2C_ADC_ADDRESS)} (using I2C)")
                else:
                    print(f"[ADC] No {I2C_ADC_MODEL} detected at {hex(I2C_ADC_ADDRESS)} via I2C")
                    self.i2c = None
            except Exception as exc:
                self.i2c = None
                print(f"[ADC] I2C initialization failed: {exc}")

        # if I2C not ready, fall back to SPI if available
        if not self.i2c_ready and HAS_SPI:
            try:
                self.spi = spidev.SpiDev()
                self.spi.open(0, 0)
                self.spi.max_speed_hz = 1350000
                print("[ADC] Using SPI ADC (spidev initialized)")
            except Exception:
                self.spi = None

    def _probe_i2c_adc(self) -> bool:
        if not self.i2c:
            return False
        try:
            command = ADS7830_COMMAND_BASE | ((TEMP_CHANNEL & 0x07) << 3)
            self.i2c.write_byte(I2C_ADC_ADDRESS, command)
            time.sleep(0.01)
            raw = self.i2c.read_byte(I2C_ADC_ADDRESS)
            return isinstance(raw, int)
        except Exception:
            return False

    def read_channel(self, channel: int) -> int:
        if self.spi:
            if channel < 0 or channel > 7:
                raise ValueError("ADC channel must be between 0 and 7")
            command = 0b11 << 6 | (channel & 0x07) << 3
            raw = self.spi.xfer2([command, 0, 0])
            value = ((raw[1] & 0x0F) << 8) | raw[2]
            return value

        if self.i2c and self.i2c_ready:
            return self.read_i2c_channel(channel)

        return 0

    def read_i2c_channel(self, channel: int) -> int:
        if channel < 0 or channel > 7:
            raise ValueError("ADC channel must be between 0 and 7")

        # ADS7830 requires a command byte with the channel encoded
        try:
            command = ADS7830_COMMAND_BASE | ((channel & 0x07) << 3)
            self.i2c.write_byte(I2C_ADC_ADDRESS, command)
            time.sleep(0.01)
            raw = self.i2c.read_byte(I2C_ADC_ADDRESS)
            return int((raw / 255.0) * ADC_MAX)
        except Exception:
            pass


        try:
            raw = self.i2c.read_byte_data(I2C_ADC_ADDRESS, ADS7830_COMMAND_BASE | ((channel & 0x07) << 3))
            return int((raw / 255.0) * ADC_MAX)
        except Exception:
            pass

        return 0

    def close(self):
        if self.spi:
            self.spi.close()
        if self.i2c:
            try:
                self.i2c.close()
            except Exception:
                pass

# functions for handling button press, toggling mode, reading values, LCD display, etc.
class SmartHomeController:
    # initialize controller
    def __init__(self):
        self.mode = NORMAL_MODE
        self.lcd = LCDManager()
        self.adc = ADCReader()
        self.last_light_value = None
        self.last_alert_time = None
        self.mode_lock = threading.Lock()
        self.gpio_button_event = False
        self.button_last_state = None
        self.last_button_time = 0.0
        self.button_debounce = 0.3

        if HAS_GPIO:
            self.setup_gpio()

    # setup GPIO and pins
    def setup_gpio(self):
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        try:
            GPIO.cleanup()
        except Exception:
            pass

        GPIO.setup(NORMAL_LED_PIN, GPIO.OUT)
        GPIO.setup(SECURITY_LED_PIN, GPIO.OUT)
        GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(TRIG_PIN, GPIO.OUT)
        GPIO.setup(ECHO_PIN, GPIO.IN)
        GPIO.output(TRIG_PIN, GPIO.LOW)
        time.sleep(0.1)

        try:
            GPIO.add_event_detect(BUTTON_PIN, GPIO.FALLING, callback=self.button_pressed, bouncetime=300)
            self.gpio_button_event = True
        except RuntimeError as exc:
            self.gpio_button_event = False
            print(f"Warning: button edge detection disabled: {exc}")
            print("Using polling fallback for button presses.")

        try:
            self.button_last_state = GPIO.input(BUTTON_PIN)
        except Exception:
            self.button_last_state = None

    # handle switching modes using button
    def button_pressed(self, channel):
        with self.mode_lock:
            self.mode = SECURITY_MODE if self.mode == NORMAL_MODE else NORMAL_MODE
            print(f"Button pressed: switching to {self.mode}")

    # handle switching modes using keyboard input
    def toggle_mode(self):
        with self.mode_lock:
            self.mode = SECURITY_MODE if self.mode == NORMAL_MODE else NORMAL_MODE
            print(f"Mode changed to {self.mode}")

    # button polling
    def poll_button(self):
        if not HAS_GPIO:
            return
        try:
            current_state = GPIO.input(BUTTON_PIN)
        except Exception:
            return

        if self.button_last_state is None:
            self.button_last_state = current_state
            return

        if self.button_last_state == GPIO.HIGH and current_state == GPIO.LOW:
            now = time.time()
            if now - self.last_button_time >= self.button_debounce:
                self.button_pressed(BUTTON_PIN)
                self.last_button_time = now

        self.button_last_state = current_state

    # convert thermistor reading to temperature in celsius
    def read_temperature_celsius(self) -> float:
        raw = self.adc.read_channel(TEMP_CHANNEL)
        if raw <= 0 or raw >= ADC_MAX:
            return float("nan")

        voltage = (raw / float(ADC_MAX)) * V_REF
        resistance = REFERENCE_RESISTOR * voltage / (V_REF - voltage)
        steinhart = resistance / NOMINAL_RESISTANCE
        steinhart = math.log(steinhart)
        steinhart /= BETA_VALUE
        steinhart += 1.0 / (NOMINAL_TEMPERATURE_C + 273.15)
        steinhart = 1.0 / steinhart
        temperature_c = steinhart - 273.15
        return temperature_c

    # read light value from photoresistor
    def read_light_value(self) -> int:
        raw = self.adc.read_channel(LIGHT_CHANNEL)
        if raw is None:
            return 0
        return max(0, min(raw, ADC_MAX))

    # measure ditance in cm from ultrasonic sensor
    def measure_distance_cm(self) -> float:
        if HAS_GPIO:
            GPIO.output(TRIG_PIN, GPIO.LOW)
            time.sleep(0.05)
            GPIO.output(TRIG_PIN, GPIO.HIGH)
            time.sleep(0.00001)
            GPIO.output(TRIG_PIN, GPIO.LOW)

            start_time = time.time()
            timeout = start_time + 0.04
            while GPIO.input(ECHO_PIN) == 0 and time.time() < timeout:
                start_time = time.time()

            stop_time = time.time()
            timeout = stop_time + 0.04
            while GPIO.input(ECHO_PIN) == 1 and time.time() < timeout:
                stop_time = time.time()

            duration = stop_time - start_time
            distance = (duration * 34300) / 2
            if distance <= 0 or distance > 400:
                return float("nan")
            return distance

        return float("nan")

    # send email alert when suspicious activity is detected
    def send_email_alert(self, event_type: str, distance: float, light_value: int):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        subject = "Raspberry Pi Security Alert"
        body = (
            f"Security Alert!\n\n"
            f"Mode: {self.mode}\n"
            f"Event: {event_type}\n"
            f"Ultrasonic distance: {distance:.1f} cm\n"
            f"Light sensor value: {light_value}\n"
            f"Time: {now}\n"
        )
        message = f"Subject: {subject}\n\n{body}"

        sender = EMAIL_SETTINGS["sender_email"]
        receiver = EMAIL_SETTINGS["receiver_email"]
        password = EMAIL_SETTINGS["password"]

        if "your.email" in sender or "recipient@example" in receiver or "your_app_password" in password:
            print("[EMAIL] Email alert would be sent here with the following content:")
            print(message)
            return

        try:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(EMAIL_SETTINGS["smtp_server"], EMAIL_SETTINGS["smtp_port"], context=context) as server:
                server.login(sender, password)
                server.sendmail(sender, receiver, message)
            print("[EMAIL] Security alert sent successfully.")
        except Exception as exc:
            print(f"[EMAIL] Failed to send alert: {exc}")

    # normal mode display on LCD
    def display_normal_state(self, temperature: float):
        now = datetime.datetime.now()
        time_str = now.strftime("%H:%M:%S")
        mode_text = "Normal Mode"
        if not math.isnan(temperature):
            temp_str = f"{temperature:.1f}C"
        else:
            temp_str = "N/A"

        # Compose second line as HH:MM:SS   xx.xC (fits 16x2 LCD)
        line2 = f"{time_str}   {temp_str}"

        self.lcd.write(mode_text, line2)
        print(f"{now.strftime('%Y-%m-%d %H:%M:%S')} | {mode_text} | {line2}")

    # security mode display on LCD
    def display_security_state(self, distance: float, light_value: int):
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mode_text = "Security Mode"
        distance_text = f"Dist: {distance:.1f} cm" if not math.isnan(distance) else "Dist: N/A"
        light_text = f"Light: {light_value}"

        self.lcd.write(mode_text, distance_text)
        print(f"{current_time} | {mode_text} | {distance_text} | {light_text}")

    # blink LED during suspicious activity
    def blink_led(self, pin: int, blink_count: int = 3, blink_time: float = 0.2):
        if not HAS_GPIO:
            return
        for _ in range(blink_count):
            GPIO.output(pin, GPIO.HIGH)
            time.sleep(blink_time)
            GPIO.output(pin, GPIO.LOW)
            time.sleep(blink_time)

    # normal mode loop
    def normal_mode_loop(self):
        temperature = self.read_temperature_celsius()
        self.display_normal_state(temperature)

        if HAS_GPIO:
            GPIO.output(NORMAL_LED_PIN, GPIO.HIGH)
            GPIO.output(SECURITY_LED_PIN, GPIO.LOW)

    # security mode loop
    def security_mode_loop(self):
        distance = self.measure_distance_cm()
        light_value = self.read_light_value()
        self.display_security_state(distance, light_value)

        if HAS_GPIO:
            GPIO.output(NORMAL_LED_PIN, GPIO.LOW)
            GPIO.output(SECURITY_LED_PIN, GPIO.HIGH)

        event_type = None
        suspicious_motion = not math.isnan(distance) and distance <= PROXIMITY_THRESHOLD_CM
        suspicious_light = False

        if self.last_light_value is None:
            self.last_light_value = light_value

        if abs(light_value - self.last_light_value) >= LIGHT_CHANGE_THRESHOLD:
            suspicious_light = True
            self.last_light_value = light_value

        if suspicious_motion or suspicious_light:
            parts = []
            if suspicious_motion:
                parts.append("Motion detected")
            if suspicious_light:
                parts.append("Light change detected")
            event_type = " + ".join(parts)
            self.handle_security_event(event_type, distance, light_value)
        else:
            self.last_light_value = light_value

    # handle security event by sending email
    def handle_security_event(self, event_type: str, distance: float, light_value: int):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("[SECURITY EVENT] Detected suspicious activity")
        print(f" Time: {timestamp}")
        print(f" Event: {event_type}")
        print(f" Distance: {distance:.1f} cm")
        print(f" Light: {light_value}")

        self.blink_led(SECURITY_LED_PIN)

        if self.last_alert_time is None or time.time() - self.last_alert_time >= ALERT_COOLDOWN_SECONDS:
            self.send_email_alert(event_type, distance, light_value)
            self.last_alert_time = time.time()
        else:
            countdown = ALERT_COOLDOWN_SECONDS - (time.time() - self.last_alert_time)
            print(f"[SECURITY EVENT] Email cooldown active for {countdown:.1f} more seconds.")

    # handle keyboard input
    def keyboard_listener(self):
        print("Keyboard control enabled. Type 'm' + Enter to switch mode, 'q' + Enter to quit.")
        while True:
            try:
                command = sys.stdin.readline().strip().lower()
                if not command:
                    continue
                if command == "m":
                    self.toggle_mode()
                elif command == "q":
                    print("Exiting program by user request.")
                    self.cleanup()
                    sys.exit(0)
            except Exception:
                break

    # clean up resources
    def cleanup(self):
        print("Cleaning up resources...")
        if self.adc:
            self.adc.close()
        if HAS_GPIO:
            GPIO.cleanup()
        self.lcd.clear()

    # main loop
    def run(self):
        keyboard_thread = threading.Thread(target=self.keyboard_listener, daemon=True)
        keyboard_thread.start()

        try:
            while True:
                with self.mode_lock:
                    mode = self.mode

                if mode == NORMAL_MODE:
                    self.normal_mode_loop()
                else:
                    self.security_mode_loop()

                if not self.gpio_button_event:
                    self.poll_button()

                time.sleep(LOOP_DELAY_SECONDS)
        except KeyboardInterrupt:
            print("Keyboard interrupt received. Shutting down...")
        finally:
            self.cleanup()


def main():
    controller = SmartHomeController()
    controller.run()


if __name__ == "__main__":
    main()
