## What and why

## Privacy impact
- [ ] No new data stored, or the data inventory (docs/compliance/data-inventory.md) is updated
- [ ] Deletion still covers every new object/record (tests updated)
- [ ] No new audit fields, or they are added to the allowlist in `src/kiosk/audit.py` with a validator

## Checks run
- [ ] `make lint test`
- [ ] `make synth synth-regions` (cdk-nag clean)
- [ ] `make demo-offline`
- [ ] `make scan`
- [ ] Deployed and ran `make verify` (say where and when), or explain why not

## Not tested
