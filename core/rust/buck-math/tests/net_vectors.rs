//! The same independently generated integer vectors as Solidity and both
//! language bindings. This vendored copy also ships with cargo package.
use buck_math::{mul_div_256, net_decay, net_fund_after, net_target_relief, U256};
use serde_json::Value;

fn uint(v: &Value) -> U256 {
    let hex = v.as_str().unwrap().strip_prefix("0x").unwrap();
    let padded = format!("{hex:0>64}");
    let mut bytes = [0; 32];
    for (i, byte) in bytes.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&padded[i * 2..i * 2 + 2], 16).unwrap();
    }
    U256::from_be_bytes(bytes)
}

#[test]
fn canonical_integer_vectors() {
    let vectors: Value =
        serde_json::from_str(include_str!("vectors/net-math-vectors.json")).unwrap();
    let cases = vectors["cases"].as_array().unwrap();
    assert_eq!(cases.len() as u64, vectors["count"].as_u64().unwrap());
    assert!(cases.len() >= 300);
    for row in cases {
        let args: Vec<_> = row["args"].as_array().unwrap().iter().map(uint).collect();
        let got = match row["op"].as_str().unwrap() {
            "decay" => net_decay(args[0], args[1]),
            "fundNetAfter" => net_fund_after(args[0], args[1], args[2]),
            "targetRelief" => net_target_relief(args[0], args[1], args[2]),
            "mulDiv" => mul_div_256(args[0], args[1], args[2], row["roundUp"].as_bool().unwrap()),
            other => panic!("unknown vector operation {other}"),
        };
        let error = row["error"].as_str().unwrap();
        if error.is_empty() {
            assert_eq!(got.unwrap(), uint(&row["value"]), "{}", row["name"]);
        } else {
            assert_eq!(got.unwrap_err().to_string(), error, "{}", row["name"]);
        }
    }
}
