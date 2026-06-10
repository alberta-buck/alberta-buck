// Debug witness generation for note_binding circuit
const fs = require("fs");
const path = require("path");

const REPO = path.resolve(__dirname, "..", "..");
const BUILD = path.join(REPO, "build", "snark", "note_binding");

function fnvHash(str) {
    const uint64_max = BigInt(2) ** BigInt(64);
    let hash = BigInt("0xCBF29CE484222325");
    for (var i = 0; i < str.length; i++) {
        hash ^= BigInt(str[i].charCodeAt());
        hash *= BigInt(0x100000001B3);
        hash %= uint64_max;
    }
    let shash = hash.toString(16);
    let n = 16 - shash.length;
    shash = '0'.repeat(n).concat(shash);
    return shash;
}

async function main() {
    const wcModule = require(path.join(BUILD, "note_binding_js", "witness_calculator.js"));
    const wasmBuffer = fs.readFileSync(
        path.join(BUILD, "note_binding_js", "note_binding.wasm")
    );

    const wc = await wcModule(wasmBuffer);
    wc.instance.exports.init(0);

    // Test each expected signal name
    const signals = [
        "nullifier",
        "eEncRx", "eEncRy", "eEncCx", "eEncCy",
        "piX", "piY",
        "rho", "idHash",
        "eNote", "eIss0",
        "s", "m_rec", "sm", "r", "rm", "b",
        "MI",
        "R0_limb", "C0_limb",
    ];

    for (const name of signals) {
        const h = fnvHash(name);
        const hMSB = parseInt(h.slice(0, 8), 16);
        const hLSB = parseInt(h.slice(8, 16), 16);
        try {
            const size = wc.instance.exports.getInputSignalSize(hMSB, hLSB);
            console.log(`  ${name}: size=${size}`);
        } catch (e) {
            console.log(`  ${name}: ERROR - ${e.message}`);
        }
    }

    // Now try setting each signal
    const input = JSON.parse(
        fs.readFileSync(path.join(BUILD, "input.json"), "utf8")
    );

    // Check which keys qualify_input produces
    console.log("\nInput keys:", Object.keys(input));

    // Flatten and check each
    function qualify_input(prefix, input, input1) {
        if (Array.isArray(input)) {
            let a = [];
            function flat(arr) {
                for (let x of arr) {
                    if (Array.isArray(x)) flat(x); else a.push(x);
                }
            }
            flat(input);
            if (a.length > 0 && typeof a[0] !== "object") {
                input1[prefix] = input;
            } else {
                for (let i = 0; i < input.length; i++) {
                    qualify_input(prefix + "[" + i + "]", input[i], input1);
                }
            }
        } else if (typeof input === "object") {
            for (const k of Object.keys(input)) {
                qualify_input(prefix === "" ? k : prefix + "." + k, input[k], input1);
            }
        } else {
            input1[prefix] = input;
        }
    }

    let flat = {};
    qualify_input("", input, flat);
    console.log("\nFlattened keys:");
    for (const k of Object.keys(flat)) {
        console.log(`  ${k}: ${Array.isArray(flat[k]) ? "array[" + flat[k].length + "]" : flat[k]}`);
    }
}

main().catch(e => { console.error(e); process.exit(1); });
