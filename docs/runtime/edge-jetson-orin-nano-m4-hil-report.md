# Jetson Orin Nano M4 HIL Acceptance Report

- Status: `pending_hardware`
- Target: Jetson Orin Nano, JetPack 7.2 / L4T 39.2, NVMe
- Software milestone: M4 `software_complete`

## Pending evidence

- fixed-IP/VLAN/ACL/firewall evidence for result and preview traffic;
- backlog creation during server and network loss;
- replay across at least two Boot IDs;
- result receipt, ResultRecord and cursor counts proving no duplicate persistence;
- outbox byte/item reduction after confirmed ACK;
- rejected sequence-gap and changed-content replay evidence;
- preview transfer only during an active viewer lease;
- lease renewal, explicit release and TTL expiry evidence;
- JPEG size, resolution, FPS and bandwidth measurements;
- encode, pull and browser relay latency;
- CPU, RAM, GPU and disk impact with zero, one and multiple viewers;
- five-minute warmup plus 30-minute 1080p/30 workload run.

Hardware acceptance remains open until the physical device, representative camera, model and
isolated plant VLAN are available. Software simulation does not replace this report.
