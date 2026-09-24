// mint_fxrp.mjs — wrap XRP into FXRP (Flare FAssets direct minting).
//
// This is step 1 of funding the desk. It sends ONE XRPL Payment to the FAssets
// Core Vault carrying a 32-byte direct-minting memo; Flare's executors then mint
// the matching FXRP to a Flare address you control.
//
//   XRPL XRP  ──Payment + memo──▶  FAssets Core Vault  ──▶  FXRP on Flare (chain 14)
//
// Safety properties (do not weaken these):
//   * DRY RUN BY DEFAULT. Nothing is signed or broadcast without --broadcast.
//   * The seed is read from the environment only. It is never written to a file,
//     never logged, and never passed as an argument.
//   * The Core Vault and every fee are resolved live from the Flare registry.
//     Nothing is hardcoded — a stale address sends your XRP nowhere.
//   * The recipient must be a Flare EOA whose key YOU hold. Minting to a
//     contract-wallet address (e.g. an exchange or Derive owner address) strands
//     the FXRP, because no session key can spend it. KNOWN_STRANDING below is a
//     real address that has already swallowed funds this way.
//
// Usage:
//   node mint_fxrp.mjs --recipient 0x<flare-eoa> --amount 30            # dry run
//   node mint_fxrp.mjs --recipient 0x<flare-eoa> --amount 30 --broadcast
//
// Env:
//   XRPL_SEED        XRPL family seed of the wallet holding the XRP   (required)
//   XRPL_RPC_URL     e.g. https://xrplcluster.com                      (required)
//   FLARE_RPC_URL    default https://flare-api.flare.network/ext/C/rpc
//   FLARE_NETWORK    "mainnet" (default) | "coston2"
import { Client, Wallet } from "xrpl";
import { createPublicClient, http } from "viem";
import { flare as flareMainnet, coston2 } from "@flarenetwork/flare-wagmi-periphery-package";

// FlareContractRegistry — same address on every Flare network.
const REGISTRY = "0xaD67FE66660Fb8dFE9d6b1b4240d8650e30F6019";

// 8-byte DIRECT_MINTING prefix, per Flare's direct-minting spec.
const DIRECT_MINTING_PREFIX = "4642505266410018";

// Addresses that have already stranded FXRP. Minting here loses the funds:
// it is a smart-contract wallet on Derive (chain 957) and a KEYLESS EOA on
// Flare (chain 14), so no key you hold can move the result.
const KNOWN_STRANDING = new Set([
  "0x895a87bf65898adffba8547d21a7c4a0797f1eaa",
]);

const UBA_PER_XRP = 1_000_000; // XRP has 6 decimals; UBA = drops

function buildDirectMintingMemo(recipient) {
  // [8-byte prefix][4-byte zero padding][20-byte recipient, no 0x, lowercase]
  return DIRECT_MINTING_PREFIX + "00000000" + recipient.slice(2).toLowerCase();
}

function argValue(args, name) {
  const i = args.indexOf(name);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : undefined;
}

/** Resolve AssetManagerFXRP and the Core Vault XRPL address live. */
async function resolveOnChain(rpcUrl, network) {
  const chain = network === "coston2" ? coston2 : flareMainnet;
  const pc = createPublicClient({ chain, transport: http(rpcUrl) });

  const assetManager = await pc.readContract({
    address: REGISTRY,
    abi: chain.iFlareContractRegistryAbi,
    functionName: "getContractAddressByName",
    args: ["AssetManagerFXRP"],
  });
  const coreVault = await pc.readContract({
    address: assetManager,
    abi: chain.iDirectMintingAbi,
    functionName: "directMintingPaymentAddress",
  });
  const fxrpToken = await pc.readContract({
    address: assetManager,
    abi: chain.iAssetManagerAbi,
    functionName: "fAsset",
  });
  return { pc, chain, assetManager, coreVault, fxrpToken };
}

/**
 * Quote the XRPL payment for a target net amount, from live on-chain settings.
 *
 *   payment = net + max(net * feeBIPS / 10000, minimumFeeUBA) + executorFeeUBA
 *
 * There is no single "compute total" getter on the AssetManager, so the three
 * inputs are read separately. All three are UBA (drops) except feeBIPS.
 */
async function quoteFees({ pc, chain, assetManager }, netXrp) {
  const ABI = chain.iDirectMintingSettingsAbi;
  const read = (fn) =>
    pc.readContract({ address: assetManager, abi: ABI, functionName: fn });

  const [bips, minFeeUBA, executorFeeUBA] = await Promise.all([
    read("getDirectMintingFeeBIPS"),
    read("getDirectMintingMinimumFeeUBA"),
    read("getDirectMintingExecutorFeeUBA"),
  ]);

  const proportional = (netXrp * Number(bips)) / 10_000;
  const minimum = Number(minFeeUBA) / UBA_PER_XRP;
  const mintFee = Math.max(proportional, minimum);
  const executorFee = Number(executorFeeUBA) / UBA_PER_XRP;

  return {
    feeBIPS: Number(bips),
    mintFee,
    executorFee,
    total: netXrp + mintFee + executorFee,
  };
}

async function main() {
  const args = process.argv.slice(2);
  const broadcast = args.includes("--broadcast");
  const network = (process.env.FLARE_NETWORK || "mainnet").toLowerCase();
  const rpcUrl =
    process.env.FLARE_RPC_URL ||
    (network === "coston2"
      ? "https://coston2-api.flare.network/ext/C/rpc"
      : "https://flare-api.flare.network/ext/C/rpc");

  const recipient = argValue(args, "--recipient") || process.env.FLARE_RECIPIENT;
  const netXrp = Number(argValue(args, "--amount") || process.env.MINT_AMOUNT_XRP || 0);

  if (!recipient || !/^0x[0-9a-fA-F]{40}$/.test(recipient)) {
    throw new Error("Pass --recipient 0x<40-hex Flare address> (a Flare EOA whose key you hold).");
  }
  if (KNOWN_STRANDING.has(recipient.toLowerCase())) {
    throw new Error(
      `REFUSING: ${recipient} is a keyless EOA on Flare and a contract wallet on ` +
        `Derive. FXRP minted here cannot be spent. Use a Flare EOA whose private ` +
        `key you hold.`
    );
  }
  if (!Number.isFinite(netXrp) || netXrp <= 0) {
    throw new Error("Pass --amount <net XRP to convert>, e.g. --amount 30");
  }

  const onchain = await resolveOnChain(rpcUrl, network);
  const quote = await quoteFees(onchain, netXrp);
  const memo = buildDirectMintingMemo(recipient);

  console.log("=== FXRP DIRECT MINT — PRE-FLIGHT ===");
  console.log("network            :", network);
  console.log("AssetManagerFXRP   :", onchain.assetManager);
  console.log("Core Vault (XRPL)  :", onchain.coreVault);
  console.log("FXRP token (Flare) :", onchain.fxrpToken);
  console.log("recipient (Flare)  :", recipient);
  console.log("memo (32 bytes)    :", memo);
  console.log("");
  console.log("net FXRP to mint   :", netXrp.toFixed(6), "XRP");
  console.log(`mint fee           : ${quote.mintFee.toFixed(6)} XRP  (${quote.feeBIPS} bips, floor applies)`);
  console.log("executor fee       :", quote.executorFee.toFixed(6), "XRP");
  console.log("XRPL payment TOTAL :", quote.total.toFixed(6), "XRP");
  console.log("");

  if (!broadcast) {
    console.log("DRY RUN — nothing signed, nothing sent. Add --broadcast to submit.");
    return;
  }

  const seed = process.env.XRPL_SEED;
  if (!seed) throw new Error("XRPL_SEED is not set. It is required only for --broadcast.");
  if (!process.env.XRPL_RPC_URL) throw new Error("XRPL_RPC_URL is not set.");

  const wallet = Wallet.fromSeed(seed);
  console.log("sender (XRPL)      :", wallet.classicAddress);

  const client = new Client(process.env.XRPL_RPC_URL);
  await client.connect();
  try {
    console.log("\nSubmitting XRPL Payment ...");
    const tx = await client.submitAndWait(
      {
        TransactionType: "Payment",
        Account: wallet.classicAddress,
        Destination: onchain.coreVault,
        Amount: quote.total.toFixed(6), // XRP is a string amount (drops are integer)
        Memos: [{ Memo: { MemoData: memo } }],
      },
      { wallet }
    );
    console.log("tx hash            :", tx.result.hash);
    console.log("engine result      :", tx.result.engine_result, tx.result.engine_result_message);
    if (tx.result.meta?.TransactionResult !== "tesSUCCESS") {
      throw new Error(`XRPL rejected the payment: ${tx.result.meta?.TransactionResult}`);
    }
    console.log(
      "\ntesSUCCESS. The mint is NOT instant: Flare executors finalise it and FXRP " +
        "appears at the recipient address shortly after. Do not re-send — poll the " +
        "FXRP balance instead."
    );
  } finally {
    await client.disconnect();
  }
}

main().catch((err) => {
  console.error("mint failed:", err.message);
  process.exit(1);
});
