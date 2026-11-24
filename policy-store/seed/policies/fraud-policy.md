# Fraud review policy (synthetic, svc-sim)

- Review every order from the orders topic and every handoff from the shopping assistant.
- Gather order details, order history for the shipping address, payment signals and the rule-based verdict before deciding.
- Approve when the verdict is approve and there is no handoff concern.
- Hold when the verdict is review or decline, or when a handoff reports social engineering with a score of 0.4 or more.
- Any hold or release for an order above the approval threshold (default 100 USD) needs human approval through the approval queue. Do not act until it is approved.
- Record every decision in the case log with the evidence used.
