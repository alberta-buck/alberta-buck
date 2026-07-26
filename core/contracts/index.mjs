// alberta-buck-contracts -- compiled ABI + bytecode for the Alberta Buck
// contracts, for any EVM: anvil, Tevm in a browser, a testnet.
//
// Nothing here is deployed at a fixed address.  Every BUCK world deploys
// fresh and learns its addresses from the receipts, so what this package
// ships is (abi, bytecode) pairs -- not a deployment registry.  The
// deployments export exists so that publishing real addresses later is not
// a breaking change to the package's shape.

import contractsJson from "./contracts.json" with { type: "json" };
import compilerJson from "./compiler.json" with { type: "json" };
import deploymentsJson from "./deployments.json" with { type: "json" };

/** Every published contract, keyed by name: { abi, bytecode, deployedBytecode }. */
export const contracts = contractsJson.contracts;

/** Build provenance: solc version, optimizer settings, git commit, bundle sha256. */
export const compiler = compilerJson;

/** Known deployments by chain id.  Empty: BUCK worlds deploy their own. */
export const deployments = deploymentsJson;

/**
 * One contract's artifact, by name.
 * @param {string} name e.g. "Buck", "BuckCredit", "IdentityRegistry"
 * @returns {{abi: object[], bytecode: string, deployedBytecode: string}}
 */
export function artifact(name) {
  const a = contracts[name];
  if (!a) {
    const known = Object.keys(contracts).join(", ");
    const external = compilerJson.external?.[name];
    if (external) {
      throw new Error(
        `${name} is not published here -- it is a third-party contract. ` +
        `Install ${external} and take it from there.`);
    }
    throw new Error(`unknown contract ${name}; this package ships: ${known}`);
  }
  return a;
}

export default contracts;
