// Browser stub for node:fs -- @tevm/node imports {existsSync, readFileSync}
// for an on-disk state-persistence path the in-browser demo never takes.
export const existsSync = () => false;
export const readFileSync = () => {
  throw new Error("fs.readFileSync is not available in the browser bundle");
};
export default { existsSync, readFileSync };
