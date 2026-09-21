// SPDX-License-Identifier: GPL-3.0
/*
    Copyright 2021 0KIMS association.

    This file is generated with [snarkJS](https://github.com/iden3/snarkjs).

    snarkJS is a free software: you can redistribute it and/or modify it
    under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    snarkJS is distributed in the hope that it will be useful, but WITHOUT
    ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
    or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public
    License for more details.

    You should have received a copy of the GNU General Public License
    along with snarkJS. If not, see <https://www.gnu.org/licenses/>.
*/

pragma solidity >=0.7.0 <0.9.0;

contract IdentityMembershipB1Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 709997778838307835264383870894323768967370809777328976677908306577656761867;
    uint256 constant alphay  = 12836297161486382489279136251040704944281163072493113933910361141757593083677;
    uint256 constant betax1  = 3684114477087397643340155885713584867253826796058431906225088186474534956028;
    uint256 constant betax2  = 21569416133113965215801657701571922097548973573516190817471638180089358098266;
    uint256 constant betay1  = 4326734521026516125659375800180265709604472291722677762145538662848384222069;
    uint256 constant betay2  = 16828383835347505433443117675638127444543738572256217033768785829213672209398;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 2440956448598645860862264688679983202229877468505569689410900943491172653674;
    uint256 constant deltax2 = 5164204468303044478542109531940284992363296759182593886785234296157607292938;
    uint256 constant deltay1 = 7452498495818946963484453168173254953000405875912468774930869508462974053875;
    uint256 constant deltay2 = 8365496822259972682529383816362183195742659727164980196802233050037795414638;

    
    uint256 constant IC0x = 17551079800983643661454649368642158004441462920044177452722452502763415911786;
    uint256 constant IC0y = 18289677294285928616856350682069969058526440558550940391735997671030404985857;
    
    uint256 constant IC1x = 9146207428650038290361242766219861969266571946429767682809666385182429425502;
    uint256 constant IC1y = 12821509238281016559025015957896294383680952544283434078530988489990684195459;
    
    uint256 constant IC2x = 5622542425369242064391920240923547182875271573361729529952647841164348745624;
    uint256 constant IC2y = 6043762513572291148918963421815547523999939541181999511718911097175091024877;
    
    uint256 constant IC3x = 12462975623618236363059063096360354616553625448475889962088858397817080738269;
    uint256 constant IC3y = 3316937267088495981355519875031120379333188533547685282858498015782212739710;
    
    uint256 constant IC4x = 7427175590096582640707797089599823698726343838357809891486276226620183212335;
    uint256 constant IC4y = 434714911659182171569612460297983406180812022858651507423827360378494699415;
    
    uint256 constant IC5x = 9533137492863733672144292253617360409163936487209154339181225849075203322907;
    uint256 constant IC5y = 10574246832702959669905355417165374362954950375107250868072801951121477195481;
    
    uint256 constant IC6x = 20725457784157868129780230818383265406291568264015147294616865005862715113842;
    uint256 constant IC6y = 11220418454040137964577519562668772158875369068346606037390179849591375922395;
    
    uint256 constant IC7x = 3424709784856222472007490764989774260560206836276350147188619379244145553023;
    uint256 constant IC7y = 21237049814043985586484830536505739247728552764940854267774343165399275147414;
    
    uint256 constant IC8x = 13358985486871883430667904969381420719840272474341954995740665572443700651571;
    uint256 constant IC8y = 20245275144566020357558777980529155450787022709186365766160029894163461212216;
    
    uint256 constant IC9x = 15188226966853207136183731145789850149570262741384674751455515977226649925744;
    uint256 constant IC9y = 4741692947876713065930614606242804055793402299446937500431568170205633803683;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[9] calldata _pubSignals) public returns (bool) {
        assembly {
            function checkField(v) {
                if iszero(lt(v, r)) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }
            
            // G1 function to multiply a G1 value(x,y) to value in an address
            function g1_mulAccC(pR, x, y, s) {
                let success
                let mIn := mload(0x40)
                mstore(mIn, x)
                mstore(add(mIn, 32), y)
                mstore(add(mIn, 64), s)

                success := staticcall(sub(gas(), 2000), 7, mIn, 96, mIn, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }

                mstore(add(mIn, 64), mload(pR))
                mstore(add(mIn, 96), mload(add(pR, 32)))

                success := staticcall(sub(gas(), 2000), 6, mIn, 128, pR, 64)

                if iszero(success) {
                    mstore(0, 0)
                    return(0, 0x20)
                }
            }

            function checkPairing(pA, pB, pC, pubSignals, pMem) -> isOk {
                let _pPairing := add(pMem, pPairing)
                let _pVk := add(pMem, pVk)

                mstore(_pVk, IC0x)
                mstore(add(_pVk, 32), IC0y)

                // Compute the linear combination vk_x
                
                g1_mulAccC(_pVk, IC1x, IC1y, calldataload(add(pubSignals, 0)))
                
                g1_mulAccC(_pVk, IC2x, IC2y, calldataload(add(pubSignals, 32)))
                
                g1_mulAccC(_pVk, IC3x, IC3y, calldataload(add(pubSignals, 64)))
                
                g1_mulAccC(_pVk, IC4x, IC4y, calldataload(add(pubSignals, 96)))
                
                g1_mulAccC(_pVk, IC5x, IC5y, calldataload(add(pubSignals, 128)))
                
                g1_mulAccC(_pVk, IC6x, IC6y, calldataload(add(pubSignals, 160)))
                
                g1_mulAccC(_pVk, IC7x, IC7y, calldataload(add(pubSignals, 192)))
                
                g1_mulAccC(_pVk, IC8x, IC8y, calldataload(add(pubSignals, 224)))
                
                g1_mulAccC(_pVk, IC9x, IC9y, calldataload(add(pubSignals, 256)))
                

                // -A
                mstore(_pPairing, calldataload(pA))
                mstore(add(_pPairing, 32), mod(sub(q, calldataload(add(pA, 32))), q))

                // B
                mstore(add(_pPairing, 64), calldataload(pB))
                mstore(add(_pPairing, 96), calldataload(add(pB, 32)))
                mstore(add(_pPairing, 128), calldataload(add(pB, 64)))
                mstore(add(_pPairing, 160), calldataload(add(pB, 96)))

                // alpha1
                mstore(add(_pPairing, 192), alphax)
                mstore(add(_pPairing, 224), alphay)

                // beta2
                mstore(add(_pPairing, 256), betax1)
                mstore(add(_pPairing, 288), betax2)
                mstore(add(_pPairing, 320), betay1)
                mstore(add(_pPairing, 352), betay2)

                // vk_x
                mstore(add(_pPairing, 384), mload(add(pMem, pVk)))
                mstore(add(_pPairing, 416), mload(add(pMem, add(pVk, 32))))


                // gamma2
                mstore(add(_pPairing, 448), gammax1)
                mstore(add(_pPairing, 480), gammax2)
                mstore(add(_pPairing, 512), gammay1)
                mstore(add(_pPairing, 544), gammay2)

                // C
                mstore(add(_pPairing, 576), calldataload(pC))
                mstore(add(_pPairing, 608), calldataload(add(pC, 32)))

                // delta2
                mstore(add(_pPairing, 640), deltax1)
                mstore(add(_pPairing, 672), deltax2)
                mstore(add(_pPairing, 704), deltay1)
                mstore(add(_pPairing, 736), deltay2)


                let success := staticcall(sub(gas(), 2000), 8, _pPairing, 768, _pPairing, 0x20)

                isOk := and(success, mload(_pPairing))
            }

            let pMem := mload(0x40)
            mstore(0x40, add(pMem, pLastMem))

            // Validate that all evaluations ∈ F
            
            checkField(calldataload(add(_pubSignals, 0)))
            
            checkField(calldataload(add(_pubSignals, 32)))
            
            checkField(calldataload(add(_pubSignals, 64)))
            
            checkField(calldataload(add(_pubSignals, 96)))
            
            checkField(calldataload(add(_pubSignals, 128)))
            
            checkField(calldataload(add(_pubSignals, 160)))
            
            checkField(calldataload(add(_pubSignals, 192)))
            
            checkField(calldataload(add(_pubSignals, 224)))
            
            checkField(calldataload(add(_pubSignals, 256)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
