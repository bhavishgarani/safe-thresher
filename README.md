## Inspiration
We were inspired by the engineering and technology used by the U.S. Navy and U.S. Coast Guard, particularly their focus on safety, reliability, and preventing equipment failures. Our goal was to help prevent future disasters, such as the flooding caused by a pipe leak aboard the USS Thresher. We wanted to develop a system that could detect potential hazards early and provide engineers with the information they need to respond before a situation becomes critical.


## What it does
Safe Thresher is designed to be installed in low-lying areas, locations highly susceptible to damage, or spaces that are difficult to access and maintain. It continuously monitors moisture levels to detect potential leaks and identify dangerous conditions before they escalate. The system transmits real-time data to an app, allowing the captain and engineering team to monitor conditions remotely and make informed decisions. Its LED indicators and buzzer also provide immediate visual and audible warnings when moisture levels reach dangerous thresholds.


## How we built it
We built Safe Thresher using an Arduino Uno R3, an HC-05 Bluetooth module, a moisture sensor, LEDs, and a buzzer. The Arduino processes the sensor readings and determines the appropriate danger level based on the detected moisture. The LEDs communicate the current risk level, while the buzzer provides an audible alert when necessary. The HC-05 Bluetooth module enables communication with an app, allowing the collected data to be monitored in real time.



## Challenges we ran into
Our biggest challenge was developing an algorithm that accurately assigns danger levels based on the moisture sensor's readings and communicates them through the LED indicators. We needed to ensure that the system could distinguish between different levels of risk and provide clear, consistent warnings. Finding the right thresholds and coordinating the sensor readings with the visual and audible alerts required careful testing and adjustments.



## Accomplishments that we're proud of
As a team, we are proud of the final result, not only because we successfully built a functional prototype, but also because of its potential to contribute to safety and save lives. We combined hardware, programming, and sensor technology to create a system designed to detect potential hazards before they become serious problems. We are especially proud that our project addresses a real-world engineering challenge and has the potential to be developed further for practical applications.


## What we learned
Throughout this experience, we learned a great deal about datasets and AI agents, with valuable guidance from the staff who supported us. We also gained hands-on experience working with microcontrollers, sensors, Bluetooth communication, and hardware integration. In addition, we learned the importance of algorithm design, testing, and teamwork when developing a system that needs to communicate potentially critical information reliably.



## What's next for Safe Thresher 
We hope to secure additional funding and access to better materials so we can conduct real-world testing and further develop Safe Thresher. Our next steps would include improving the accuracy and reliability of the moisture detection system, testing it in challenging environments, and refining the app's data visualization and alert capabilities. With further development and testing, we hope to move Safe Thresher closer to becoming a practical tool for detecting leaks and improving safety in marine environments.


