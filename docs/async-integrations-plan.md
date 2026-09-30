# Async application integrations

Add reusable integrations driven by a real Prefect/Kafka/MongoDB application:
count and interrupt actual MongoDB durable writes, bind Prefect recovery without
reimplementing application decisions, and exercise Kafka publication/acknowledgement
boundaries where the adopter needs them. Keep each integration optional.

The stack begins with MongoDB write interruption and its real-server controls,
then adds the transport and scheduler bindings needed by the adopter. Every adapter
must demonstrate a failing control as well as a convergent application. Supported
boundaries and limitations belong alongside the implementation documentation.
