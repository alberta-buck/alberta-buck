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

contract NoteBindingGroth16Verifier {
    // Scalar field size
    uint256 constant r    = 21888242871839275222246405745257275088548364400416034343698204186575808495617;
    // Base field size
    uint256 constant q   = 21888242871839275222246405745257275088696311157297823662689037894645226208583;

    // Verification Key data
    uint256 constant alphax  = 20901312453931986983070371975751072566398443441960436590636518297529489694139;
    uint256 constant alphay  = 21697131480926335546036040991413011755153140230317664815989518148158724370212;
    uint256 constant betax1  = 4869487231785253308212429570614741443191402866210819877611793555879218415484;
    uint256 constant betax2  = 8494223465375928023871052138064538434365863294294498107508764578998413175191;
    uint256 constant betay1  = 13692213518456195651330939167802977105042737180923530430942149449680303886878;
    uint256 constant betay2  = 6329045905833941605730059747255744716806210507261219104759569890353192015645;
    uint256 constant gammax1 = 11559732032986387107991004021392285783925812861821192530917403151452391805634;
    uint256 constant gammax2 = 10857046999023057135944570762232829481370756359578518086990519993285655852781;
    uint256 constant gammay1 = 4082367875863433681332203403145435568316851327593401208105741076214120093531;
    uint256 constant gammay2 = 8495653923123431417604973247489272438418190587263600148770280649306958101930;
    uint256 constant deltax1 = 1662696574816065357519395146345973357843236776917228636238893853006264243658;
    uint256 constant deltax2 = 14986279238431440907573783722242685371462781263897565157695397259985855856998;
    uint256 constant deltay1 = 10381476939777869915060559580109358214439477849601591245810401715033292193936;
    uint256 constant deltay2 = 20632050521861473477633205875646552282015832726210615749131639567163932974033;

    
    uint256 constant IC0x = 21879882208473594156409311056800800448811939227297788067686554071859521928248;
    uint256 constant IC0y = 352588294295398844112956499007920521744141812194970119145244794509727389603;
    
    uint256 constant IC1x = 7149821162005984131182571135271607501875524097807344593193472833350712815796;
    uint256 constant IC1y = 14802397561713970850414875189744245562136581217655332519299716990183443080167;
    
    uint256 constant IC2x = 12002606699350363829294273882813656210797323763622841455710679184056773931261;
    uint256 constant IC2y = 906189591220698422312582350148655851092653398465546719256549231307832520672;
    
    uint256 constant IC3x = 20454034629295902868869235419966452957018376900376598451836693019381967248092;
    uint256 constant IC3y = 5857626428986190242104139761571441344230941765959184349544674206721999458362;
    
    uint256 constant IC4x = 1156598917461065943706073114884004778279453427991175190474462333745621503526;
    uint256 constant IC4y = 13698004503849712959804528132829525957287219575234742708773667051347630165558;
    
    uint256 constant IC5x = 1098491487512090200127432309546989425781948770611900011748987440082450298345;
    uint256 constant IC5y = 6631903103892394999904149598790499846785814463287212909426418494876924392923;
    
    uint256 constant IC6x = 11289911964571773505800064129426509391734128432776435649668786939506089313388;
    uint256 constant IC6y = 19700700553481567978247621543251358730116254149716970852551636754435442315540;
    
    uint256 constant IC7x = 7842413719449707999249589437277223476729947411100219480973130965288544433327;
    uint256 constant IC7y = 14708381691170737453789597433661199875416377399221728557900891675183117590783;
    
    uint256 constant IC8x = 1826258163129373829071787161899268707438327943930468571058243095465393707677;
    uint256 constant IC8y = 6286986597884300885065594473924685768211965736347396738613595147773492535247;
    
    uint256 constant IC9x = 1800489139265773509998813686912389521936194352732736393632513167289773201217;
    uint256 constant IC9y = 15788472604817844491291027094514213723683380256005844182394910651645787918291;
    
    uint256 constant IC10x = 17364867732083613213867687403012501343591234000182563102023908414250699326780;
    uint256 constant IC10y = 4403983908678566604180173131260827821308874375886351373302121515799713197433;
    
    uint256 constant IC11x = 18989258407423331359853499901403204274558177117512976864859377779068731464450;
    uint256 constant IC11y = 2606468411378286825495294662822144710801836746423521892980270226072698099865;
    
    uint256 constant IC12x = 14815734498883161656299190326327406650742461748558539366763379360804220710585;
    uint256 constant IC12y = 12766562513486078675645167351156180105669171469413705177528312228508009361598;
    
    uint256 constant IC13x = 5798918337122711662225791037781743234194318573431583431751882268445911443804;
    uint256 constant IC13y = 10527913327134629471087660831265736239879293522275314087921312991734837065624;
    
    uint256 constant IC14x = 9897305094444274124356613379440528065282813220002648888417747972224860819341;
    uint256 constant IC14y = 2729736759587794566785899714162629506983333911021008491922335001368308032081;
    
    uint256 constant IC15x = 7360364364530209718283423400309837269208753501481628452852350601267223217444;
    uint256 constant IC15y = 2536872994394096400718872169578580154585107907971553159977151618524543935970;
    
    uint256 constant IC16x = 20919685857836888009565962447144431779243502802557015735516608999499049115865;
    uint256 constant IC16y = 3215380144999442202198955790995378595248518465881752169799219190838741523031;
    
    uint256 constant IC17x = 19465006025054003309662899926532704899548570431387672070264144334524846281591;
    uint256 constant IC17y = 13446840518411251092526240808908681640320939081007789222278629080433079128157;
    
    uint256 constant IC18x = 13440158323303747768390456121348553004421878356076918916847121584832149558569;
    uint256 constant IC18y = 11483532571747049203598372918218486962394582851044570397167562600054651553576;
    
    uint256 constant IC19x = 13290253179925899376086964057383192948843699810774719855214285875479424485314;
    uint256 constant IC19y = 18780960288363356687429498320717705115173415117058092166333502470849491657864;
    
    uint256 constant IC20x = 14129090454441672678746828733419724547090341996703951035465242882138208799168;
    uint256 constant IC20y = 13003856619697283312197342928278904476159315559002905629323137300542014411075;
    
    uint256 constant IC21x = 12186538295222567472021036050355308349302497908984202578902385166907195624374;
    uint256 constant IC21y = 10434319440837699055637267909017165453930196659050790657626311478836713520876;
    
    uint256 constant IC22x = 15864664559331009082038221904450403059518825182597781133673415360721286236533;
    uint256 constant IC22y = 10205077276303157467879302378032627063532512052862754646272526155847595772669;
    
    uint256 constant IC23x = 14750528771607192444184474520447923501723476351509753144658402888469006286877;
    uint256 constant IC23y = 13543497001113534409424334727233807988489727597719952160989871274076261861309;
    
    uint256 constant IC24x = 14007169365337376233226633862850608102626902053448139630202563600153850782238;
    uint256 constant IC24y = 9610263320281725296618489585186007476899037429594853714881849192798846048694;
    
    uint256 constant IC25x = 17568926775640516970697936089295421014170613550347130406212566686123128070490;
    uint256 constant IC25y = 15460402848826794123972870078324646831886141542245320227276742519412836828240;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[25] calldata _pubSignals) public returns (bool) {
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
                
                g1_mulAccC(_pVk, IC21x, IC21y, calldataload(add(pubSignals, 640)))
                
                g1_mulAccC(_pVk, IC22x, IC22y, calldataload(add(pubSignals, 672)))
                
                g1_mulAccC(_pVk, IC23x, IC23y, calldataload(add(pubSignals, 704)))
                
                g1_mulAccC(_pVk, IC24x, IC24y, calldataload(add(pubSignals, 736)))
                
                g1_mulAccC(_pVk, IC25x, IC25y, calldataload(add(pubSignals, 768)))
                

                // -A
                mstore(_pPairing, calldataload(pA))
                mstore(add(_pPairing, 32), mod(sub(q, calldataload(add(pA, 32))), q))

                // B
                mstore(add(_pPairing, 64), calldataload(add(pB, 32)))
                mstore(add(_pPairing, 96), calldataload(pB))
                mstore(add(_pPairing, 128), calldataload(add(pB, 96)))
                mstore(add(_pPairing, 160), calldataload(add(pB, 64)))

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
            
            checkField(calldataload(add(_pubSignals, 640)))
            
            checkField(calldataload(add(_pubSignals, 672)))
            
            checkField(calldataload(add(_pubSignals, 704)))
            
            checkField(calldataload(add(_pubSignals, 736)))
            
            checkField(calldataload(add(_pubSignals, 768)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
