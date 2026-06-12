// Generate witness for note_binding circuit
const fs = require("fs");
const path = require("path");

const REPO = path.resolve(__dirname, "..", "..");
const BUILD = path.join(REPO, "build", "snark", "note_binding");

async function main() {
    const wc = require(path.join(BUILD, "note_binding_js", "witness_calculator.js"));
    const input = JSON.parse(
        fs.readFileSync(path.join(BUILD, "input.json"), "utf8")
    );

    console.error("Input keys:", Object.keys(input));
    console.error("Input field count:", Object.keys(input).length);

    try {
        const wtns = await wc.calculateWTNSBin(input);
        fs.writeFileSync(path.join(BUILD, "witness.wtns"), Buffer.from(wtns));
        console.log("Witness generated successfully, size:", wtns.byteLength);
    } catch (e) {
        console.error("Error:", e.message);
        console.error(e.stack);
        process.exit(1);
    }
}

main();
