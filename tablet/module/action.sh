#!/system/bin/sh
# Toggle manager flag only; serialized monitor performs verified recovery.
MODDIR=${0%/*}
if [ -e "$MODDIR/disable" ]; then rm "$MODDIR/disable"; else touch "$MODDIR/disable"; fi
