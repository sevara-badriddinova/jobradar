#!/bin/bash
# Run in authenticated Cloud Shell after reviewing the plan.
set -euo pipefail
cd "$(dirname "$0")"
PROJECT=jobradar-510801
ZONE=us-east1-b
VM=jobradar-vm
RUNTIME=jobradar-runtime@jobradar-510801.iam.gserviceaccount.com
cat <<'EOF'
One e2-micro VM in us-east1-b; Debian 12; 30 GB pd-standard; ephemeral IPv4.
Private custom VPC/subnet. Inbound SSH only through Google IAP, no database ports.
Reuse jobradar-runtime and its existing secret/health-bucket permissions.
Grant that identity read-only access to this project's jobradar image repository.
No workers start automatically until migration-complete is explicitly recorded.
EOF
[[ ${1:-} == --apply ]] || exit 0
GC=(gcloud --project="$PROJECT" --quiet)
"${GC[@]}" services enable compute.googleapis.com iap.googleapis.com
"${GC[@]}" compute instances list --format='table(name,zone.basename(),machineType.basename(),status)'
if "${GC[@]}" compute instances describe "$VM" --zone="$ZONE" >/dev/null 2>&1; then
  echo 'VM already exists; refusing duplicate provisioning.' >&2
  exit 1
fi
"${GC[@]}" artifacts repositories add-iam-policy-binding jobradar --location=us-east1 \
  --member="serviceAccount:${RUNTIME}" --role=roles/artifactregistry.reader
if ! "${GC[@]}" compute networks describe jobradar-vpc >/dev/null 2>&1; then
  "${GC[@]}" compute networks create jobradar-vpc --subnet-mode=custom
fi
if ! "${GC[@]}" compute networks subnets describe jobradar-subnet --region=us-east1 >/dev/null 2>&1; then
  "${GC[@]}" compute networks subnets create jobradar-subnet --network=jobradar-vpc \
    --region=us-east1 --range=10.66.0.0/24
fi
if ! "${GC[@]}" compute firewall-rules describe jobradar-iap-ssh >/dev/null 2>&1; then
  "${GC[@]}" compute firewall-rules create jobradar-iap-ssh --network=jobradar-vpc \
    --direction=INGRESS --action=ALLOW --rules=tcp:22 --source-ranges=35.235.240.0/20 \
    --target-tags=jobradar-vm
fi
"${GC[@]}" compute instances create "$VM" --zone="$ZONE" --machine-type=e2-micro \
  --image-family=debian-12 --image-project=debian-cloud --boot-disk-type=pd-standard \
  --boot-disk-size=30GB --no-boot-disk-auto-delete --deletion-protection \
  --network=jobradar-vpc --subnet=jobradar-subnet --tags=jobradar-vm \
  --service-account="$RUNTIME" --scopes=cloud-platform \
  --metadata=block-project-ssh-keys=TRUE \
  --metadata-from-file=startup-script=bootstrap.sh \
  --labels=app=jobradar,runtime=single-vm,cost-tier=free-eligible \
  --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring
