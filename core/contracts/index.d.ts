export interface Artifact {
  abi: object[];
  bytecode: string;
  deployedBytecode: string;
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
}

export declare const contracts: Record<string, Artifact>;
export declare const compiler: Compiler;
export declare const deployments: Record<string, Record<string, string>>;
export declare function artifact(name: string): Artifact;
export default contracts;
