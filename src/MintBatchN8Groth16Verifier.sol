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
    uint256 constant deltax1 = 11115333124137647874404196691762842225998508337704358363112412185344852666846;
    uint256 constant deltax2 = 9951835198162424519197744483695069767948487119766151599874267012664775692832;
    uint256 constant deltay1 = 3469558049393745067032118887343578407384539963307606705650861331743567932682;
    uint256 constant deltay2 = 1408295077482718227886615280391652195748185023761654412583493337084274087353;

    
    uint256 constant IC0x = 712071328703826117843001484493897119460972867014498256404785679259663722945;
    uint256 constant IC0y = 6513138317459366184433600167312273306416201209177507145292055375397079225721;
    
    uint256 constant IC1x = 5738795308734211373500900366070495597052602235904604116662946977440374667871;
    uint256 constant IC1y = 17854142083992866691898056691319342861656652245258030396903674933610946166923;
    
    uint256 constant IC2x = 17651521013579900656078463820896410238564442788154061835851956341720007810048;
    uint256 constant IC2y = 3055773159526823642618793629549899455073582743848687574989448151953588364108;
    
    uint256 constant IC3x = 2658644845995948044644269223345439376926862719122186714180350033938448187653;
    uint256 constant IC3y = 14229809169011891516589257021440900942690785323511378104334916414995246558519;
    
    uint256 constant IC4x = 3919140376204728700503487920206391958415451492488825340211047711591734333648;
    uint256 constant IC4y = 12455876777244131868203436173319719024175301432382648926172605701590154469841;
    
    uint256 constant IC5x = 5468151636710779371181319897683181258233695160354983639491003811995105452386;
    uint256 constant IC5y = 8204700732541942798424363384767131339982833189579973001963844810459379870886;
    
    uint256 constant IC6x = 15905630697370923083487300891107611468825103602714825685493907302072983330836;
    uint256 constant IC6y = 51679661653169777367258918957352867455539174178595788400195462322281234627;
    
    uint256 constant IC7x = 8604714044955261409325476684216886769105164249358043651635280951675510580350;
    uint256 constant IC7y = 16104322274895348899213233888127873122905406985983181349833348554108140817341;
    
    uint256 constant IC8x = 15316695322106684230635352963780544251073645844393645056703956380020052517381;
    uint256 constant IC8y = 3330082450626519544123173568918440274282419623105991317259375283990123452886;
    
    uint256 constant IC9x = 19565719020065334977558343636006366849605112271179785908337269290034800383175;
    uint256 constant IC9y = 10915654156727539047923704571363514066863841084664324709561876245088331529871;
    
    uint256 constant IC10x = 9997508398577044819680656561794122385996346076024059486960767230900607153166;
    uint256 constant IC10y = 7144046002657535131737896281780888665118840789080751756834181302983705523489;
    
    uint256 constant IC11x = 6837745917160289952632221341394313334744585416834326779549433348049792895928;
    uint256 constant IC11y = 17127426118237891677362004855313649788866072344441726430868130642723750767363;
    
    uint256 constant IC12x = 795709725140493381728941324535097384437567513888298356500341167699291368802;
    uint256 constant IC12y = 17643183786024357076834914233407520822388357688347213420654155051391624132538;
    
    uint256 constant IC13x = 431621841778343474056990229833808220356118560148729344167468632991632143686;
    uint256 constant IC13y = 13878117207881793665892177874192201479922357364093962247569650776990343176926;
    
    uint256 constant IC14x = 6262418611135164551566329749249316543513322922949340010012865614039052840182;
    uint256 constant IC14y = 4224900250013405716804129499291332714008027965466664865673154491494693937320;
    
    uint256 constant IC15x = 4516932223938217602318359923852281223105662739704020039663357209963765510532;
    uint256 constant IC15y = 18531027438800508942290006504083224667449975384387883253686430789096885012978;
    
    uint256 constant IC16x = 4570574247741172514161779472068649745785212235288716655387666031079614438950;
    uint256 constant IC16y = 4396897466415592208890887982565576086719584849521908087331370976275380792267;
    
    uint256 constant IC17x = 12333563762770838306982279302089525062919582519516444806753907681488324080412;
    uint256 constant IC17y = 14163780978722353654077962548117894779388437237185428337628173917613942555289;
    
    uint256 constant IC18x = 21080536176909762479285518282475840817164769090293746556631268578835781279704;
    uint256 constant IC18y = 16482911463028754370762787835388507109433361802434861694288570840964943278160;
    
    uint256 constant IC19x = 18503223914101130406172537871136112938085224794233332075175367008319818680764;
    uint256 constant IC19y = 17487350923957737478099069476070353786582448040782407217496600108519762143729;
    
    uint256 constant IC20x = 14479492194240094573247711962023219681950818089884051717291465948888938280547;
    uint256 constant IC20y = 18657303604908179822496575055931812205013354512671925185149963989726274719179;
    
 
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
