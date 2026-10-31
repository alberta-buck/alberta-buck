// Experimental BuckNet deployment and per-NFT operations.  This module is
// browser-safe: callers inject artifacts, identities and transaction dispatch.
// A deployed stack is not usable until the Carrying fund is bound and a basket
// configured; premium-bearing facilities also need their insurer's own pool.
import { linkArtifact } from "./artifacts.js";

export async function deployNetStack(session, artifacts, { registry, credit, controller, governance,
                                                         creditDeployer, registryGovernor } = {}) {
  const gov = governance?.address ?? governance ?? session.account.address;
  const deploy = (name, args = []) => session.deploy(artifacts(name), args, { name });
  const fund = await deploy("JubileeFund", [gov]);
  const maturityLibrary = await deploy("BuckNetMaturity");
  const transfersLibrary = await deploy("BuckNetTransfers");
  const linked = linkArtifact(artifacts("BuckNet"), {
    "src/lib/BuckNetMaturity.sol:BuckNetMaturity": maturityLibrary.address,
    "src/lib/BuckNetTransfers.sol:BuckNetTransfers": transfersLibrary.address,
  });
  const token = await session.deploy(linked, [credit.address, controller.address, registry.address, fund.address],
    { name: "BuckNet" });
  const quoter = await deploy("BuckNetQuoter", [token.address]);
  await session.send(credit, "setBuck", [token.address], { account: creditDeployer });
  await session.send(registry, "setBuck", [token.address], { account: registryGovernor });
  return { registry, credit, controller, fund, token, quoter, maturityLibrary, transfersLibrary };
}

/** Actual per-facility amounts; never synthesize a signed debt from a wallet.
 *  execute(owner, contract, method, args) can dispatch through an agent proxy.
 *  Quotes are advisory. Authorization and reduction are separate transactions;
 *  a failed reduction leaves its prior authorization to retry or cancel. */
export function netCreditOps(session, stack, execute = null) {
  const { credit, token, fund, quoter } = stack;
  const send = execute ?? ((owner, target, name, args) => session.send(target, name, args, { account: owner }));
  const facility = async (tokenId) => {
    const owner = await session.call(credit, "ownerOf", [tokenId]);
    const [[face, activated, premiumRate], [issued, claimSeconds], premiumNominal, insurerPool, spendable] =
      await Promise.all([session.call(credit, "creditInfo", [tokenId]), session.call(credit, "issuanceOf", [tokenId]),
        session.call(token, "premiumPrincipal", [tokenId]), session.call(token, "facilityPool", [tokenId]),
        session.call(token, "balanceOf", [owner])]);
    return { tokenId, owner, face, activated, premiumRate, issued, claimSeconds, premiumNominal, insurerPool, spendable };
  };
  const quoteIssue = async (tokenId, coverage) => {
    const owner = await session.call(credit, "ownerOf", [tokenId]);
    const [[presentCoverage, premium, reserveRequired, maxSpendable], ownerSpendable] = await Promise.all([
      session.call(quoter, "quoteIssue", [tokenId, coverage]), session.call(token, "balanceOf", [owner]),
    ]);
    const reserveShortfall = reserveRequired > ownerSpendable ? reserveRequired - ownerSpendable : 0n;
    return { presentCoverage, premium, reserveRequired, maxSpendable, ownerSpendable, reserveShortfall };
  };
  return {
    facility, quoteIssue,
    issue: (owner, id, coverage, spendable) => send(owner, token, "issue", [id, coverage, spendable]),
    quoteReduction: (id, principal) => session.call(fund, "previewRelief", [id, principal]),
    authorizeReduction: (owner, id, principal, minimumReliefScaled, deadline) =>
      send(owner, token, "authorizeReduction", [id, principal, minimumReliefScaled, deadline]),
    reduceAuthorized: (owner, id, principal) => send(owner, fund, "reduce", [id, principal]),
  };
}
