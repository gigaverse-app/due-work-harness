# Async application integrations

Add reusable integrations driven by a real Prefect/Kafka/MongoDB application:
count and interrupt actual MongoDB durable writes, bind Prefect recovery without
reimplementing application decisions, and exercise aiokafka consumer-offset
acknowledgement boundaries where the adopter needs them. Keep each integration optional.

The stack begins with MongoDB write interruption and its real-server controls,
then adds the transport and scheduler bindings needed by the adopter. Every adapter
must demonstrate a failing control as well as a convergent application. Supported
boundaries and limitations belong alongside the implementation documentation.

The consumer-offset adapter is client-specific: `integrations.aiokafka`, installed
with the `[aiokafka]` extra. It does not support other Kafka clients or producer
publication faults. Broker-backed tests exercise actual offset acknowledgement and replay.
