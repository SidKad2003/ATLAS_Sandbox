"""Create svc-sim Kafka topics on the shop's broker (idempotent)."""
import asyncio
import os

from aiokafka.admin import AIOKafkaAdminClient, NewTopic

TOPICS = ["agent.events", "agent.handoff"]


async def main() -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=os.environ.get("KAFKA_ADDR", "kafka:9092"))
    for attempt in range(30):
        try:
            await admin.start()
            break
        except Exception as exc:
            print(f"kafka not ready ({exc}), retry {attempt}")
            await asyncio.sleep(2)
    try:
        existing = set(await admin.list_topics())
        new = [NewTopic(name=t, num_partitions=1, replication_factor=1) for t in TOPICS if t not in existing]
        if new:
            await admin.create_topics(new)
        print("topics ready:", ", ".join(TOPICS))
    finally:
        await admin.close()


if __name__ == "__main__":
    asyncio.run(main())
