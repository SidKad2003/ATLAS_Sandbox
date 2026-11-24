#!/usr/bin/env bash
# Prints SSM port-forward commands. Run them on your admin workstation, not on the host.
cat <<'EOF'
Run each in its own terminal on your admin workstation (needs the Session Manager plugin):

  # Shop, Jaeger (/jaeger/ui), Grafana (/grafana), flagd-ui (/feature)
  aws ssm start-session --region eu-west-2 --target i-0123456789abcdef0 \
    --document-name AWS-StartPortForwardingSession --parameters 'portNumber=8080,localPortNumber=8080'

  # Approval UI
  aws ssm start-session --region eu-west-2 --target i-0123456789abcdef0 \
    --document-name AWS-StartPortForwardingSession --parameters 'portNumber=8090,localPortNumber=8090'

  # sim-viz (store town)
  aws ssm start-session --region eu-west-2 --target i-0123456789abcdef0 \
    --document-name AWS-StartPortForwardingSession --parameters 'portNumber=8091,localPortNumber=8091'

Then open http://localhost:8080, http://localhost:8090 and http://localhost:8091.
SSH users can instead use: ssh -L 8080:127.0.0.1:8080 -L 8090:127.0.0.1:8090 -L 8091:127.0.0.1:8091 svc-sim-host
EOF
