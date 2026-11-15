# Vendored packages

`alberta-buck-webevm-0.9.1-clock.1.tgz` is webevm (MIT, github.com/wighawag/webevm) 0.9.1 plus
the node's clock and its time cheats under anvil's names (`evm_setNextBlockTimestamp`,
`evm_increaseTime`, `evm_mine`, `anvil_mine`, `anvil_setBlockTimestampInterval`), which every
world here needs. It is built from our fork, github.com/pjkundert/webevm branch
`feature/block-clock-cheats` (commit 07507d2), offered upstream as
https://github.com/wighawag/webevm/pull/23, and packed under our own name: `webevm` on npm is
upstream's, and we never publish under it. Its `dist` and `src` are the fork's build unchanged;
only `package.json` (name, version, the fork as repository, `albertaBuckSource` naming the
commit) and a notice atop upstream's README differ.

`core/js/package.json` installs it as `webevm` (the dependency's key names the folder), so every
`import ... from "webevm"` is unchanged.

To rebuild it from a clean checkout of the fork (~/src/webevm by default):

    make nix-webevm-fork-pack               # scripts/webevm-fork.mjs; WEBEVM_FORK, WEBEVM_FORK_VERSION

## Publishing, until upstream releases the clock

A published `alberta-buck-core` cannot carry a `file:` dependency (the release workflow refuses
one), so before core's next release:

1. Publish this tarball once, by hand (npm offers trusted publishing only for a package that
   exists): `npm login` as the alberta-buck packages' maintainer, then
   `make nix-webevm-fork-publish` (`npm publish <tgz> --access public --tag latest`).
2. Point core at it by alias, and relock:
   `"webevm": "npm:alberta-buck-webevm@0.9.1-clock.1"`, then `npm install` in core/js.
3. Delete the tarball here.

When upstream releases the clock: `"webevm": "^<that version>"`, relock, and deprecate ours
(`npm deprecate alberta-buck-webevm "use webevm >= <that version>"`).
