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

contract MintBatchN8Groth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 5222163254956769720861560326401075741386994847086210558405366783377756092216;
    uint256 constant alphay  = 19475436559984623554357953129009359053333542731694135281922495325049604221230;
    uint256 constant betax1  = 3059516489220240630186271195914183140946064923114682236776148214202533588863;
    uint256 constant betax2  = 4726638434264925561921847005269901493966648479077249500650785888021982438689;
    uint256 constant betay1  = 20695777602333613504155353915066064094342222466636704397235542841177800495176;
    uint256 constant betay2  = 4930004227668553506141963663146714216566264478789620269782604620259282120034;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 21437143376972714670570671152519007923087920487515459169966888185260783000507;
    uint256 constant deltax2 = 21689126791591206672656047213749273623039837175456497639517718266018387132350;
    uint256 constant deltay1 = 517452540968732153875115225312684073956845034385906809399331569546349743231;
    uint256 constant deltay2 = 10121580450019380290071403302933497219330697608929108960348411511806284300592;

    
    uint256 constant IC0x = 10389618319842269432761017376153023610593594976152515755648426316024394726181;
    uint256 constant IC0y = 473662022335687912000836893915324555073094741131454427136789862283420992323;
    
    uint256 constant IC1x = 81523916480471976111975936246821748536221012169547770364310756398175841916;
    uint256 constant IC1y = 13278219355846649872371426640426519049891583243005752087827882706282021017868;
    
    uint256 constant IC2x = 7289358243344873822478925353958473469089561337184035000628123694496614081489;
    uint256 constant IC2y = 3481454512296105267943906717485892174252463096605340423692485377556058856619;
    
    uint256 constant IC3x = 10895861407747443446234663127256448537044719865147472533948050234077319864344;
    uint256 constant IC3y = 18924512845406898058356842198841262447552995399935863578271869263089184929584;
    
    uint256 constant IC4x = 1711099960496761949123384719686440971238961095044386454924180239213586794503;
    uint256 constant IC4y = 15471102225972341337186886391173694586156593320764349419627505124517780629776;
    
    uint256 constant IC5x = 13284175905387210469214512965647377409091560061989955720018305165007254590791;
    uint256 constant IC5y = 19156338502101989890361592869106490220628680565727516217214010299298340739484;
    
    uint256 constant IC6x = 7591421728008504093452332907258705047648693606796141793583242397443280112487;
    uint256 constant IC6y = 19059490445321660757592602519171891316436550392141185023240482383756355071061;
    
    uint256 constant IC7x = 14130812634708005071365575717169052226951845937546938904192205584177031806015;
    uint256 constant IC7y = 11844031865150119614592591732273938428321623713159888679615457319319237688623;
    
    uint256 constant IC8x = 5825370550182588843753976864687073281688957567811802372471616247393583007974;
    uint256 constant IC8y = 11274835114429227759443155394122813330643726758003285562583041938686893664627;
    
    uint256 constant IC9x = 5937165332007429919909889442398202059570114523733986825349728860929336947374;
    uint256 constant IC9y = 19625746745262376416933486865086684611838555796218059555608241446037931530845;
    
    uint256 constant IC10x = 11690567046740391318786060514728601983680639031762528999914843180834995613181;
    uint256 constant IC10y = 12596031480871870534635757211131830633761184222647644721456190906696595359102;
    
    uint256 constant IC11x = 19384682007707815178212500684913528586980711327990887549837748981764862795619;
    uint256 constant IC11y = 14632750272943632688779028578408187384059169478730134620916565083535361973917;
    
    uint256 constant IC12x = 4747289360020114009764683009497490298174693191119542868274457508620409793830;
    uint256 constant IC12y = 10306357851488710616483901872868961426154856029855177505495104413979535269052;
    
    uint256 constant IC13x = 18295365358850155078857137450337449634615328481574223130572430262397261730947;
    uint256 constant IC13y = 21037416333831255594124356252399703226500714761416928631370248087254545359012;
    
    uint256 constant IC14x = 19848499415876785713790843110857101924076655430020773794064598550374072324907;
    uint256 constant IC14y = 20149510290205739647621311000815718100747128886698560466532258477942630760895;
    
    uint256 constant IC15x = 15360071887066333977172205764639957665413797147316361272841144089668733505059;
    uint256 constant IC15y = 1398071485305828845515933938276471301008838894458148486296186043466268483545;
    
    uint256 constant IC16x = 14892256307054437378287861341943809087289826066217703699719128400702616335242;
    uint256 constant IC16y = 17764353047823857748485667922539815506747105185591583243808179998351533785480;
    
    uint256 constant IC17x = 17339407414998531748004371921453569538377088148403625824338347903977471716498;
    uint256 constant IC17y = 711515311188804199826084281479290619699829998590215912985583237506573282917;
    
    uint256 constant IC18x = 16266459058497653254083998430507266623593068238634797907329574938696824473189;
    uint256 constant IC18y = 1089970357941707644373410244829638963900069044993084194617529664098410859516;
    
    uint256 constant IC19x = 9848500145158739097637348491391930072483851225939251558804528697888050962234;
    uint256 constant IC19y = 9924380185114468199979335933163693085999912421692340538542189962095307740919;
    
    uint256 constant IC20x = 3318143384146893637014089670163987184379234527837818974372867696120766182743;
    uint256 constant IC20y = 1263410016747682702124311937625336342433956058672433729225579623818420225872;
    
 
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
