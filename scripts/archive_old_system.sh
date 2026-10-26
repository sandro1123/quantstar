#!/bin/bash
# Archive old quantstar system
set -euo pipefail
echo "Archiving old quantstar system..."
if [ -d ~/quantstar ] && [ ! -d ~/quantstar-archive ]; then
    mv ~/quantstar ~/quantstar-archive
    echo "  ✅ ~/quantstar → ~/quantstar-archive"
else
    echo "  ⏭️ Already archived or not found"
fi
# Remove old test directory
if [ -d ~/quantstar_test ]; then
    rm -rf ~/quantstar_test
    echo "  ✅ ~/quantstar_test removed"
fi
echo "Done"