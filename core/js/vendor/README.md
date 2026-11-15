# Our webevm build: alberta-buck-webevm

Nothing is vendored here now. `core/js/package.json` depends on

    "webevm": "npm:alberta-buck-webevm@0.9.1-clock.1"

an npm alias: the package installs as `node_modules/webevm`, so every `import ... from "webevm"`
is unchanged. `alberta-buck-webevm` is webevm (MIT, github.com/wighawag/webevm) 0.9.1 plus the
node's clock and its time cheats under anvil's names (`evm_setNextBlockTimestamp`,
`evm_increaseTime`, `evm_mine`, `anvil_mine`, `anvil_setBlockTimestampInterval`), which every world
here needs. It is built from our fork, github.com/pjkundert/webevm branch
`feature/block-clock-cheats` (commit 07507d2), offered upstream as
https://github.com/wighawag/webevm/pull/23, and published under our own name: `webevm` on npm is
upstream's, and we never publish under it. Its `dist` and `src` are the fork's build unchanged;
only `package.json` (name, version, the fork as repository, `albertaBuckSource` naming the commit)
and a notice atop upstream's README differ. Published 2026-11-15 (npm staged publishing: the
maintainer approves a staged version on npmjs.com before npm serves it).

## A new build of the fork

From a clean checkout of the fork (~/src/webevm by default), with a new version:

    make nix-webevm-fork-pack WEBEVM_FORK_VERSION=0.9.1-clock.2     # scripts/webevm-fork.mjs -> here
    make nix-webevm-fork-publish WEBEVM_FORK_VERSION=0.9.1-clock.2  # after npm login; then approve it

then point the alias at the new version, `npm install` in core/js, and delete the tarball here.

## When upstream releases the clock

`"webevm": "^<that version>"`, relock, and deprecate ours:
`npm deprecate alberta-buck-webevm "use webevm >= <that version>"`.
