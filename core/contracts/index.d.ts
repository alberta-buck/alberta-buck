export interface Artifact {
  abi: object[];
  bytecode: string;
  deployedBytecode: string;
  /** Present on every Groth16 verifier: until v1.0.0 its setup's toxic waste is public. */
  trustedSetup?: "development";
}

/** The Groth16 verifiers' setup.  Development: anyone can forge a proof they accept. */
export interface TrustedSetup {
  kind: "development";
  until: string;
  entropy: string;
  consequence: string;
  purpose: string;
  verifiers: string[];
}

export interface Compiler {
  solc: string;
  viaIR: boolean;
  optimizer: { enabled?: boolean; runs?: number };
  evmVersion: string;
  commit: string;
  sha256: string;
  /** Third-party contracts a BUCK world also needs, and the package to get them from. */
  external: Record<string, string>;
  trustedSetup: TrustedSetup;
  /** Contracts not produced by solc (the Poseidon hashers), and where their code comes from. */
  generated: Record<string, { source: string; abi: string; generator: string }>;
}

export declare const contracts: Record<string, Artifact>;
export declare const compiler: Compiler;
export declare const deployments: Record<string, Record<string, string>>;
export declare function artifact(name: string): Artifact;
export default contracts;
