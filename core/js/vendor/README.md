# Vendored packages

`webevm-0.9.1-clock-07507d2.tgz` is `npm pack` of webevm (MIT, github.com/wighawag/webevm) built
from the fork github.com/pjkundert/webevm, branch `feature/block-clock-cheats`, commit 07507d2,
offered upstream as https://github.com/wighawag/webevm/pull/23: upstream 0.9.1 plus the node's
clock and its time cheats under anvil's names (`evm_setNextBlockTimestamp`, `evm_increaseTime`,
`evm_mine`, `anvil_mine`, `anvil_setBlockTimestampInterval`), which every world here needs.
It is pinned by `core/js/package.json` (`file:vendor/...`) until upstream releases the clock; then
depend on the published version and delete this file.

To refresh from a checkout of the fork:

    cd ~/src/webevm/packages/webevm && corepack pnpm build && corepack pnpm pack --pack-destination <here>

A published `alberta-buck-core` cannot carry a `file:` dependency: replace it before the next
release of that package.
