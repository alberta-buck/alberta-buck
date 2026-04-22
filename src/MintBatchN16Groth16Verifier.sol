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

contract MintBatchN16Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 7978374530620261980907563326553864501965289527526872291934547527166537747551;
    uint256 constant alphay  = 5901113449839617362854698006124082792885893177043162830865317277785118545420;
    uint256 constant betax1  = 18220824476844239577768066887046680990910624075694529208134813320434359817912;
    uint256 constant betax2  = 16450672241897021868604598054257760272991953374496313798435261611576668251269;
    uint256 constant betay1  = 11726201621433110351792550440521544935687171988284287970664993912055275635786;
    uint256 constant betay2  = 20979770059934782849226372365594771099262336599978723952245345619213052609192;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 9032715929376470568342654471751376228321801624755517830988303860318688794168;
    uint256 constant deltax2 = 9735404408855244495190221637838009335211521989894124528939140865688430462167;
    uint256 constant deltay1 = 19924224742441261563539135369057601113931425502423566799606181093297663668250;
    uint256 constant deltay2 = 20409289366645234277049428039017254842197534185581493982735424593991182533113;

    
    uint256 constant IC0x = 16123051491559050370429417649946784713363890681526131094399216663391177429803;
    uint256 constant IC0y = 5495491752161695587297499170674882735691271533108512804754358821179885321821;
    
    uint256 constant IC1x = 19782423885906306843164742536195600981427167081078688500278945293129188933625;
    uint256 constant IC1y = 17813886216182935240963579215429000541492450820994733532652439682156106494771;
    
    uint256 constant IC2x = 18793942648821627545902849104802731038533022241136545480048616077046684382467;
    uint256 constant IC2y = 1441415850935300994361771841642057155610333288221810027581591061066489456405;
    
    uint256 constant IC3x = 8517093137957036839010171373805966364346658497671384158944157453415506797600;
    uint256 constant IC3y = 2001121396994737614047650104004479285149270994673523615633018873407857008669;
    
    uint256 constant IC4x = 16474009662709670798427236513294976353111656925131531668514070490089032030974;
    uint256 constant IC4y = 15863415770419270496420514285164702448175004936338915444162400345510296537116;
    
    uint256 constant IC5x = 439602011032811269106020844416235161417600936771019334069390831082118214463;
    uint256 constant IC5y = 14278494132035138359966808893058527247460127159473625330001875862952698880187;
    
    uint256 constant IC6x = 14552036048677677596101134229379380576086549706531124360089433901308472428283;
    uint256 constant IC6y = 1079381422199099584220230259969393060112291479663127862985394119360052461412;
    
    uint256 constant IC7x = 16080213021513047462587458656531965031078299221622285410420032282631814225131;
    uint256 constant IC7y = 20421965024977636378250727199231750720427482879522355614140958263485507286133;
    
    uint256 constant IC8x = 3330350550350552607841824204446012607213395622325414003096624526341457281233;
    uint256 constant IC8y = 41473731562173045800267972064282925128218104476331894903007756455461501615;
    
    uint256 constant IC9x = 12706817654799439513577503561166191331715008533607133283469339019479828556172;
    uint256 constant IC9y = 8151299743078136841286183549512283767882864268313441278281204930323422877479;
    
    uint256 constant IC10x = 15245041956657654606445733022915613980424793804654513502511181452322125421688;
    uint256 constant IC10y = 20647397334739225575876655889821282097887036437037284353193080137844534931686;
    
    uint256 constant IC11x = 15417740497032874468245931658933901500167879496518823147402201727792025340109;
    uint256 constant IC11y = 11453662569473563969360243582485253901193876660537591960167011642522129854435;
    
    uint256 constant IC12x = 21015480574217027302545628735507401852946860292350950025674505660888661465761;
    uint256 constant IC12y = 6951254680593799332938833233319615094488266582965810471398946542764504303827;
    
    uint256 constant IC13x = 6654125213100656418021896056537381463830933521269901891573075152592265656740;
    uint256 constant IC13y = 8224240313831977071112256683510505537234796976457892369280162819713215272570;
    
    uint256 constant IC14x = 11958552119540513176515028878287092868804514013566363870823338699329480157064;
    uint256 constant IC14y = 9166058808964832906585042665071153992665495367030684428134248686732992184280;
    
    uint256 constant IC15x = 18770098582894170338283666839485433482123961074474382162235717875614197238462;
    uint256 constant IC15y = 21524625265311786858093026750163409484816384512682028917433746803357340661872;
    
    uint256 constant IC16x = 7779228439181805260825635450292252438365553014593972671689753889890671150953;
    uint256 constant IC16y = 18243733404367234596204887800933463397064868054725101147296179821844335743764;
    
    uint256 constant IC17x = 18222411472554355517790449863691040974415524695812084672348196500542364992506;
    uint256 constant IC17y = 13818883439797518226107905753587363698859001591046794596116735307125621933331;
    
    uint256 constant IC18x = 1440732250049466322093323440795710137069657473039004932922362380188662112270;
    uint256 constant IC18y = 21646847146993806893978285981802960770942622935359908548686218364742030720063;
    
    uint256 constant IC19x = 3928925229068213952743445501749666947266810159801178629567092301379364882428;
    uint256 constant IC19y = 7796862528790848289292640089201219996093198221182510195810197939554899585026;
    
    uint256 constant IC20x = 21676863878869100412008936567979540637327962367930102630716812713478364685132;
    uint256 constant IC20y = 16782074040654459531682628760327448565854541823536343205866437066293878335124;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[20] calldata _pubSignals) public view returns (bool) {
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
                
                g1_mulAccC(_pVk, IC10x, IC10y, calldataload(add(pubSignals, 288)))
                
                g1_mulAccC(_pVk, IC11x, IC11y, calldataload(add(pubSignals, 320)))
                
                g1_mulAccC(_pVk, IC12x, IC12y, calldataload(add(pubSignals, 352)))
                
                g1_mulAccC(_pVk, IC13x, IC13y, calldataload(add(pubSignals, 384)))
                
                g1_mulAccC(_pVk, IC14x, IC14y, calldataload(add(pubSignals, 416)))
                
                g1_mulAccC(_pVk, IC15x, IC15y, calldataload(add(pubSignals, 448)))
                
                g1_mulAccC(_pVk, IC16x, IC16y, calldataload(add(pubSignals, 480)))
                
                g1_mulAccC(_pVk, IC17x, IC17y, calldataload(add(pubSignals, 512)))
                
                g1_mulAccC(_pVk, IC18x, IC18y, calldataload(add(pubSignals, 544)))
                
                g1_mulAccC(_pVk, IC19x, IC19y, calldataload(add(pubSignals, 576)))
                
                g1_mulAccC(_pVk, IC20x, IC20y, calldataload(add(pubSignals, 608)))
                

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
            
            checkField(calldataload(add(_pubSignals, 288)))
            
            checkField(calldataload(add(_pubSignals, 320)))
            
            checkField(calldataload(add(_pubSignals, 352)))
            
            checkField(calldataload(add(_pubSignals, 384)))
            
            checkField(calldataload(add(_pubSignals, 416)))
            
            checkField(calldataload(add(_pubSignals, 448)))
            
            checkField(calldataload(add(_pubSignals, 480)))
            
            checkField(calldataload(add(_pubSignals, 512)))
            
            checkField(calldataload(add(_pubSignals, 544)))
            
            checkField(calldataload(add(_pubSignals, 576)))
            
            checkField(calldataload(add(_pubSignals, 608)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
