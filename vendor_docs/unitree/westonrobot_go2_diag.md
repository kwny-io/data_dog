[Skip to main content](#__docusaurus_skipToContent_fallback)

[![Logo](/img/wr-logo.png)![Logo](/img/wr-logo.png)

**Weston Robot**](/)

Products

* [Robots](/robot/intro)
* [Peripherals](/peripheral/intro)
* [Systems](/system/intro)

[Solutions](/solution/intro)[Guides](/tutorial/intro)[Support](/support/before-you-contact-us)

[GitHub](https://github.com/westonrobot)[Legacy Doc](https://westonrobot.github.io/docs-legacy/)

* [Guides](/tutorial/intro)
* [Before You Operate](/tutorial/operational-safety)
* [Software Setup](/tutorial/installation/apt_source)
* [UGV Guides](/tutorial/agilex/ugv_base_control)
* [Quadruped Guides](/tutorial/unitree/go2_diag_guide)

  + [Go2 Diagnostics Guide](/tutorial/unitree/go2_diag_guide)
  + [Go2 & Go2-W](/tutorial/unitree/go2_slam)
  + [B2 Diagnostics Guide](/tutorial/unitree/b2_diag_guide)
* [Humanoid Guides](/tutorial/unitree/g1_dev_guide)
* [Manifold Scanner Guides](/tutorial/manifold/)

* Quadruped Guides
* Go2 Diagnostics Guide

On this page

# Go2 Diagnostics Guide

The Go2 robot is a sophisticated quadruped system with 12 degrees of freedom, composed of multiple integrated components including sensors, actuators, and control software. Malfunctions can arise from either hardware faults or software anomalies. When the robot does not behave as expected, it is essential to first isolate and identify the root cause. This guide provides instructions for performing initial diagnostics and gathering key information that can be shared with our support team to facilitate effective troubleshooting and assistance.

## Behavior Analysis & Inspection[​](#behavior-analysis--inspection "Direct link to Behavior Analysis & Inspection")

When the robot does not behave as expected, the first step is to carefully observe its behavior and conduct a visual inspection. We strongly recommend setting up a dedicated workspace with continuous video recording (e.g., surveillance cameras). This is especially useful for capturing intermittent or unpredictable issues that may occur when you're not actively monitoring the system.

If the event was not recorded, you can still examine the robot post-incident for signs of malfunction. Begin with a thorough visual inspection focused on the following areas:

* **Robot Body**: Check for any visible damage, such as cracks, dents, or impact marks that may indicate collisions or falls.
* **Sensors**: Ensure all sensors are clean, unobstructed, firmly mounted, and properly connected. Dust or misalignment can cause degraded performance.
* **Cables and Connectors**: Inspect for loose plugs, frayed wires, or damaged insulation. Confirm that all connections are secure and seated properly.
* **Battery**: Look for signs of swelling, leakage, or deformation. Ensure the battery is correctly installed and shows no abnormal wear.
* **LED Indicators**: Observe the head pilot lamp for status indication:
  + **Green Flash**: Switching on
  + **Green Solid**: Powered on, obstacle avoidance enabled
  + **Blue Solid**: Obstacle avoidance disabled
  + **Purple Solid**: Companion mode
  + **Blue Slow Flash**: Motor & IMU calibration in progress
  + **Yellow Slow Flash**: Low battery warning (will crouch within 10 minutes)
  + **Red Slow Flash**: System abnormality, boot failure, hardware failure
  + **Red Fast Flash**: Motor & IMU calibration failed

## Mobile App Diagnostics Data[​](#mobile-app-diagnostics-data "Direct link to Mobile App Diagnostics Data")

You may also use the Unitree mobile app to check the robot's diagnostic information. You can download the Unitree mobile app with the QR code on [this page](https://www.unitree.com/app/go2):

![Unitree mobile app download](/assets/images/go2_app_download-b6b2704e8f03ce3dbcd5b80fce79d563.png)

To acquire the robot's diagnostic information, you need to connect your mobile device to the robot first. Go2 supports both AP mode and Wi-Fi mode. Unitree provides [tutorial videos](https://www.unitree.com/app/go2) for adding Go2 and using app features. The following screenshots quickly guide you through the process of connecting to the robot using AP mode.

![Select Robot](/assets/images/go2_app_conn_01-3dadc5114593fa0e6e525cf5f8f632ee.jpg)

Step 1. Select the Robot

![AP or Wi-Fi](/assets/images/go2_app_conn_02-c18c5500dc6e4996b408f1a5655c366d.jpg)

Step 2. Choose AP Mode

![Create Network](/assets/images/go2_app_conn_03-f356879972b00aaaad0497e3e7529691.jpg)

Step 3. Create the Robot's AP Password

![Connect Device](/assets/images/go2_app_conn_04-8f463b624f85963b59fd9d59128510ac.jpg)

Step 4. Connect Device to the Robot's AP

Once connected, you can access the robot's diagnostic information by navigating to `Device`->`Data`:

|  |  |
| --- | --- |
| ![Left Top](/assets/images/g1_app_robot_data_01-ddfe80025cbfa9fc9a69c20b052fc212.jpg) | ![Middle Top](/assets/images/g1_app_robot_data_02-212578bbe4410cc43d95651725dd2c8c.jpg) |

### Robot Alarm Information[​](#robot-alarm-information "Direct link to Robot Alarm Information")

![Robot alarm information in the Unitree mobile app](/assets/images/g1_alarm_info-e393cf75191cba9f3e58bed6a5f2f0b6.jpg)

### Robot Motor Information[​](#robot-motor-information "Direct link to Robot Motor Information")

|  |  |
| --- | --- |
| ![Left Top](/assets/images/go2_motor_information_01-f1c775260fe8378d9963a4b9fa4698c7.jpg) | ![Middle Top](/assets/images/go2_motor_information_02-a052d490a3ac7967eec72b6ce566f69e.jpg) |
| ![Left Bottom](/assets/images/go2_motor_information_03-7168206e4c4be69c742b2cc6d76970ac.jpg) | ![Middle Bottom](/assets/images/go2_motor_information_04-c60accfb05be8d2151e32a2923190e74.jpg) |
| ![Left Bottom](/assets/images/go2_motor_information_05-dd1b32685abd9b1c9ee1ef89b3a7a2c4.jpg) |  |

## General Troubleshooting[​](#general-troubleshooting "Direct link to General Troubleshooting")

This guide provides general instructions to troubleshoot, identify, and resolve common issues.

1. **Initial Startup Position**
   Ensure the robot is in this position prior to startup.

   ![Go2 startup position](/assets/images/go2_startup_position-e2560473646811be1088a7aefcda7ffc.png)
   1. Place the robot onto a flat and levelled ground.
   2. Ensure each leg joint is hand closed as shown in the image above.
   3. Ensure 2 points of contact for each leg.
2. **Using Mobile App for Diagnostic Check**
   After binding the robot to your account, check for any warnings, errors, or potential signs of failure in the app.

   * Check for robot updates and upgrade to the latest firmware for bug fixes and new features.

     ![Go2 firmware check](/assets/images/unitree_firmware_update-4fcb7c2b0086d23a60b02a6db3b9aa2c.png)

     ***Note**: For EDU versions, updates may affect software development. Refer to [Unitree Go2 Documentation](https://support.unitree.com/home/en/developer/about_Go2) for more information.*
   * Calibrate the **IMU** and **motors** only when deemed necessary *(e.g., robot unable to walk in a straight line, limping, or tilted)*. Refer to the [official tutorial videos](https://www.unitree.com/app/go2).

     ![Go2 calibration](/assets/images/quadruped_calibration-fef41b4f27b843890de506eac06bba4d.png)
   * Adjust the payload configuration [`Device`->`Data`] if additional components or devices are mounted onto the robot.
3. **Hardware Check**

   * Replace any foot pads with bald spots or excessive wear.
   * Move each joint through its range of motion.
   * Listen for any unusual sounds that may indicate mechanical issues.

## Reporting the Issue[​](#reporting-the-issue "Direct link to Reporting the Issue")

When reaching out to our support team, please provide a structured and detailed account of the problem. Please consider the following questions when reporting the issue:

* What actions or commands were executed just before the issue occurred?
* How did the robot behave during and immediately after the incident?
* Were there any error messages, warning tones, or abnormal movements? (*Alarms may be reset when the robot is restarted.*)
* Are there any visible signs of physical damage or wear?

Please attach screenshots taken from the mobile app, including diagnostic data and alarm information. If available, include relevant video footage. This helps us understand and reproduce the issue more effectively.

Understanding and documenting the issue clearly will help prevent further damage and ensure safe continuation of your development or testing efforts.

> Before transporting the robot to Weston Robot Office for servicing, please **unbind** the robot from your mobile app account.
> ***Note**: Serial Number of the robot is located in the battery compartment and through the app.*
>
> ![Go2 SN](/assets/images/unitree_robot_sn-0380a53dfae44828bcce2457ebdc051d.png)

**Tags:**

* [Unitree](/tutorial/tags/unitree "Guides for Unitree platforms.")
* [Go2](/tutorial/tags/go2 "Guides that apply to the Unitree Go2 and Go2-W quadrupeds.")
* [Diagnostics](/tutorial/tags/diagnostics "Identifying faults and reading diagnostic information.")

[Edit this page](https://github.com/westonrobot/docs/edit/main/tutorial/unitree/go2_diag_guide.md)

Last updated on **Jul 30, 2026**

[Previous

Ranger Mini Steering Calibration](/tutorial/agilex/ranger_mini_calibration)[Next

Go2 & Go2-W](/tutorial/unitree/go2_slam)

* [Behavior Analysis & Inspection](#behavior-analysis--inspection)
* [Mobile App Diagnostics Data](#mobile-app-diagnostics-data)
  + [Robot Alarm Information](#robot-alarm-information)
  + [Robot Motor Information](#robot-motor-information)
* [General Troubleshooting](#general-troubleshooting)
* [Reporting the Issue](#reporting-the-issue)

Copyright © 2026 Weston Robot Pte. Ltd. All rights reserved.
