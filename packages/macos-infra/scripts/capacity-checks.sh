#!/bin/bash
# Re-run CON-189's findings on the real target host.
#
# The original spike ran on a laptop with Tart 2.32.1. Three of its conclusions
# are load-bearing for the design and all three are properties of this host and
# this Tart version rather than of the spike's machine, so they are worth
# re-confirming where sessions will actually run:
#
#   1. `tart clone` gives each clone a distinct MAC but the *same* ECID, and
#      that is inert -- two clones still boot and network concurrently.
#   2. The concurrency ceiling is exactly two macOS VMs, refused by the kernel.
#   3. Suspending a VM releases a slot, which is what makes a 2-VM host usable.
#
# Destructive only to VMs whose names begin `cap-`, which it creates and removes.
set -uo pipefail

BASE="${BASE:-ghcr.io/cirruslabs/macos-tahoe-base:latest}"
TART="$HOME/.local/bin/tart"
CLONES=(cap-a cap-b cap-c)
BOOT_TIMEOUT_SECONDS="${BOOT_TIMEOUT_SECONDS:-240}"

failures=0
step() { printf '\n== %s\n' "$1"; }
ok() { printf '  ok  %s\n' "$1"; }
bad() {
  printf '  FAIL %s\n' "$1"
  failures=$((failures + 1))
}

cleanup() {
  step "cleanup"
  for name in "${CLONES[@]}"; do
    $TART stop "$name" 2> /dev/null
    $TART delete "$name" 2> /dev/null && echo "  deleted $name"
  done
  pkill -f 'tart run cap-' 2> /dev/null
  return 0
}
trap cleanup EXIT

step "host and versions"
printf 'host      %s (%s)\n' "$(hostname)" "$(sysctl -n hw.model)"
printf 'macos     %s\n' "$(sw_vers -productVersion)"
printf 'tart      %s\n' "$($TART --version)"
printf 'base      %s\n' "$BASE"

# A stale clone from an interrupted run would make the ceiling test lie.
cleanup > /dev/null 2>&1
trap cleanup EXIT

step "clone the base image three times"
for name in "${CLONES[@]}"; do
  start=$(date +%s%N)
  if ! $TART clone "$BASE" "$name" 2>&1; then
    bad "could not clone $name"
    exit 1
  fi
  elapsed_ms=$((($(date +%s%N) - start) / 1000000))
  echo "  cloned $name in ${elapsed_ms}ms"
done
ok "three clones exist"

step "finding 1: clones share an ECID but get distinct MACs"
# Read with sed rather than a JSON parser on purpose: this host has no working
# `python3`. /usr/bin/python3 is a Command Line Tools shim, and with no tools
# installed it prints an install notice to stderr and nothing to stdout -- so a
# parser here fails *silently* and every value compares equal, which is how the
# first run of this script reported a false pass. config.json is a single flat
# object, so sed is sufficient and cannot be absent.
#
# The whitespace tolerance is not defensive padding, it is required. Tart writes
# two different shapes: the *first* clone of an image gets the base's config
# copied verbatim -- compact, and carrying the base's own MAC -- while later
# clones get a regenerated MAC written by Tart's encoder, pretty-printed with
# spaces around the colon. A pattern matching only one shape reads half the
# clones as empty.
field() {
  tr -d '\n' < "$1" | sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p"
}

declare -a ecids macs
for name in "${CLONES[@]}"; do
  config="$HOME/tart_home/vms/$name/config.json"
  # The ECID is a base64-encoded plist; comparing the encoded blobs across
  # clones is exactly the comparison we want.
  ecid=$(field "$config" ecid)
  mac=$(field "$config" macAddress)
  if [ -z "$ecid" ] || [ -z "$mac" ]; then
    bad "could not read identity out of $config"
    continue
  fi
  ecids+=("$ecid")
  macs+=("$mac")
  printf '  %-6s mac=%s ecid=%s...\n' "$name" "$mac" "${ecid:0:24}"
done
unique_ecids=$(printf '%s\n' "${ecids[@]}" | sort -u | wc -l | tr -d ' ')
unique_macs=$(printf '%s\n' "${macs[@]}" | sort -u | wc -l | tr -d ' ')
if [ "$unique_ecids" = "1" ]; then
  ok "all three clones share one ECID, as CON-189 found"
else
  bad "expected 1 shared ECID, got $unique_ecids distinct (behaviour changed since 2.32.1)"
fi
if [ "$unique_macs" = "3" ]; then
  ok "each clone has its own MAC"
else
  bad "expected 3 distinct MACs, got $unique_macs"
fi

boot() {
  local name="$1"
  nohup $TART run "$name" --no-graphics > "$HOME/cap-$name.log" 2>&1 &
  echo "  booting $name (pid $!)"
}

wait_for_ip() {
  $TART ip "$1" --wait "$BOOT_TIMEOUT_SECONDS" 2> /dev/null
}

step "finding 2: two boot concurrently, a third is refused"
boot cap-a
boot cap-b
address_a=$(wait_for_ip cap-a) || bad "cap-a never got an address"
address_b=$(wait_for_ip cap-b) || bad "cap-b never got an address"
echo "  cap-a $address_a"
echo "  cap-b $address_b"
if [ -n "$address_a" ] && [ -n "$address_b" ] && [ "$address_a" != "$address_b" ]; then
  ok "both VMs run at once on independent addresses"
else
  bad "two concurrent VMs did not both get distinct addresses"
fi
# `tart ip` returns as soon as a DHCP lease exists, which is earlier than the
# guest answering ICMP, so this waits rather than asking once.
reachable() {
  local address="$1" deadline=$((SECONDS + 60))
  while [ $SECONDS -lt $deadline ]; do
    ping -c 1 -t 5 "$address" > /dev/null 2>&1 && return 0
    sleep 3
  done
  return 1
}
for pair in "cap-a|$address_a" "cap-b|$address_b"; do
  name=${pair%%|*}
  address=${pair#*|}
  if [ -n "$address" ] && reachable "$address"; then
    ok "$name answers on the network at $address"
  else
    bad "$name never answered a ping at ${address:-<no address>}"
  fi
done

# The third boot must fail, and fail for the documented reason rather than any
# reason -- a timeout or a missing image would pass a naive "it failed" check.
echo "  attempting a third VM, which should be refused"
third_output=$($TART run cap-c --no-graphics 2>&1 &
  sleep 25
  pkill -f 'tart run cap-c' 2> /dev/null
  wait
  true)
if printf '%s' "$third_output" | grep -qi "exceeds the system limit"; then
  ok "third VM refused with the kernel's VM limit message"
elif [ -n "$(TART=$TART; $TART ip cap-c 2> /dev/null)" ]; then
  bad "a THIRD VM booted -- the two-VM ceiling does not hold on this host"
else
  bad "third VM did not boot, but not with the expected limit message"
  printf '       output: %s\n' "$(printf '%s' "$third_output" | tail -2)"
fi

step "finding 3: suspending one releases a slot"
if ! $TART suspend cap-a 2>&1; then
  bad "could not suspend cap-a"
else
  ok "cap-a suspended"
fi
sleep 5
boot cap-c
address_c=$(wait_for_ip cap-c)
if [ -n "$address_c" ]; then
  ok "cap-c booted into the slot suspend released ($address_c)"
else
  bad "cap-c still could not boot after a suspend -- the capacity model in CON-174/CON-177 depends on this"
fi

step "result"
if [ "$failures" = "0" ]; then
  echo "all CON-189 findings hold on this host with Tart $($TART --version)"
else
  echo "$failures check(s) disagree with CON-189"
fi
exit $((failures == 0 ? 0 : 1))
