// REAL Uniswap periphery for JS worlds: the Universal Router deploy
// recipe the Python sim proved on anvil (deploy.py), and the
// V3_SWAP_EXACT_IN encoding (mirroring alberta_buck/sim/router.py).
//
// The pools were always real (UniswapV3Factory/Pool); SimLP only stood in
// for the periphery.  Doctrine: SimLP remains for simulation god-modes
// (whale pinning, liquidity seeding); agent and USER swaps go through the
// real router -- the code path an integrator copies, and the surface real
// JS AMM tooling (@uniswap/v3-sdk et al.) can be pointed at.
//
// Wiring facts (proven by the Python sim, mirrored here):
//   * permit2 is a DUMMY address: swaps use the pre-fund route
//     (transfer the input to the router, execute with payerIsUser=false),
//     so Permit2 is never consulted.  Real Permit2 is optional follow-up
//     for signature-based approvals.
//   * poolInitCodeHash = keccak of OUR compiled UniswapV3Pool creation
//     bytecode -- the router computes pool addresses via CREATE2, and the
//     locally-built pool hash differs from mainnet's canonical one.
//   * in BUCK worlds the router must be identity-bound public+carrying
//     (it transiently custodies BUCK mid-route); deploy.py shows the bind.

import { encodeAbiParameters, encodePacked, keccak256 } from "viem";

export const ZERO_ADDR = "0x" + "00".repeat(20);
export const DUMMY_PERMIT2 = "0x" + "be".repeat(20);   // pre-fund route: unused

/**
 * Deploy the Universal Router from the vendored artifact
 * (alberta_buck/sim/artifacts/UniversalRouter.json), wired exactly as the
 * Python sim wires it.  RouterParameters field NAMES come from the
 * artifact's ABI; the VALUES follow deploy.py's order.
 *
 * @param session     a Session (deployer pays)
 * @param urArtifact  the parsed UniversalRouter.json artifact
 * @param opts.weth         WETH9 address (unused by V3 swaps; real for fidelity)
 * @param opts.v3Factory    our UniswapV3Factory address
 * @param opts.poolInitCode our UniswapV3Pool CREATION bytecode (0x-hex)
 */
export async function deployUniversalRouter(session, urArtifact,
    { weth, v3Factory, poolInitCode, gas = 15_000_000n }) {
  const ctor = urArtifact.abi.find((e) => e.type === "constructor");
  const comps = ctor.inputs[0].components;
  const vals = [DUMMY_PERMIT2, weth, ZERO_ADDR, v3Factory,
                "0x" + "00".repeat(32), keccak256(poolInitCode),
                ZERO_ADDR, ZERO_ADDR, ZERO_ADDR, ZERO_ADDR];
  if (comps.length !== vals.length) {
    throw new Error(`UniversalRouter constructor has ${comps.length} ` +
                    `params; the deploy.py recipe wires ${vals.length}`);
  }
  const params = Object.fromEntries(comps.map((c, i) => [c.name, vals[i]]));
  // Accept the raw forge-artifact shape ({bytecode: {object}}) and the
  // flattened bundle shape ({bytecode: "0x.."}) alike.
  const bytecode = urArtifact.bytecode.object ?? urArtifact.bytecode;
  return session.deploy(
    { abi: urArtifact.abi, bytecode },
    [params], { name: "UniversalRouter", gas });
}

/** [tokenA, fee0, tokenB, fee1, tokenC, ...] -> packed V3 path bytes. */
export function encodePath(tokensFees) {
  const types = tokensFees.map((_, i) => (i % 2 === 0 ? "address" : "uint24"));
  return encodePacked(types, tokensFees);
}

/**
 * (commands, inputs) for UniversalRouter.execute V3_SWAP_EXACT_IN on the
 * pre-fund route (payerIsUser=false: the router pays from its own
 * balance, so transfer amountIn to the router first).
 */
export function urExecArgs(recipient, amountIn, path) {
  const input = encodeAbiParameters(
    [{ type: "address" }, { type: "uint256" }, { type: "uint256" },
     { type: "bytes" }, { type: "bool" }, { type: "uint256[]" }],
    [recipient, amountIn, 0n, path, false, []]);
  return ["0x00", [input]];   // 0x00 = V3_SWAP_EXACT_IN
}
