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

contract NoteBindingA1Groth16Verifier {
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
    uint256 constant deltax1 = 14351196362136631019280032830121140335078375036303920405027056927679002239010;
    uint256 constant deltax2 = 18159082806572006795276708280564443186299983059912292469780953067562098958570;
    uint256 constant deltay1 = 18873507184164809619641717448753260398318580324246503877290277613126108165501;
    uint256 constant deltay2 = 18867680655104802687492933884783247502431770086929507521394098876253180631797;

    
    uint256 constant IC0x = 619703643125257011561276418425995370741915952364019328768965020206295701293;
    uint256 constant IC0y = 6881060847220522104363353942378373394035591704283863462989088758103073244441;
    
    uint256 constant IC1x = 6225731652987058514486921565380484549245391188791128921788368188017502337305;
    uint256 constant IC1y = 12870532792783408322976951415237286011110814280534277393135377863303599557635;
    
    uint256 constant IC2x = 6810393353505133845254689532957385873317002067303858694878817475024689287968;
    uint256 constant IC2y = 21278149513160053299084752026919561347171562440565307747377770411432397585997;
    
    uint256 constant IC3x = 10613280274295695935569147038913283934741600939827711678730831713941467634123;
    uint256 constant IC3y = 14511683533393962462941000832524971096897476041639041261074165004863762822248;
    
    uint256 constant IC4x = 39822890883628576237736589196112275741843346818360838800551782274586081181;
    uint256 constant IC4y = 17614561171391031334909671935796588322827958801122891046060906839565611568469;
    
    uint256 constant IC5x = 11926935721904468090651027783578394405455426664137645209304865862586857023616;
    uint256 constant IC5y = 4326130940324521024461474902428105018697541636712796006336994858235753708150;
    
    uint256 constant IC6x = 605006980996182371850782569435825659496343278581728605204307887660836588713;
    uint256 constant IC6y = 403170107286063205222644118271088863029569059903099263121286233416386947280;
    
    uint256 constant IC7x = 17108808754106788457716678706868978572420028276225290890862432591536138193412;
    uint256 constant IC7y = 5645068736363431600110422558157351227144235342108655145859861283467774651450;
    
    uint256 constant IC8x = 18390537542134700686481314421689690442579124679155282437811919037141434860617;
    uint256 constant IC8y = 9972032757218735102233197832237097918241010948261458769496012944213493626597;
    
    uint256 constant IC9x = 15336126731601905252663240998262866958738404928448370639882889100452168054380;
    uint256 constant IC9y = 10491504430535733851564867494922743950045425561949168943580382012723061158224;
    
    uint256 constant IC10x = 6766619177592994461782175512766702234890248967358678817525350282199696847842;
    uint256 constant IC10y = 3303499234925658460084287109686185584534618594864423736230509854999936568274;
    
    uint256 constant IC11x = 4791623306044656813252134359060054284802192979061585237178965446826160695278;
    uint256 constant IC11y = 15050231032443410552983930014535644703283917303773402303975736864842444969303;
    
    uint256 constant IC12x = 18288505159943331914448185452267097039670323816737880388957845781588918728508;
    uint256 constant IC12y = 18353627999274323357289930686302741449446317623746144378914944724293678707563;
    
    uint256 constant IC13x = 13991160255234371419011225504320673290470887335455000244989280738545285302360;
    uint256 constant IC13y = 676721081276551318267696021722342783107951305818337916793027564326336636187;
    
    uint256 constant IC14x = 12345553147279854420530333940852412984638595294542682682492007831117644885608;
    uint256 constant IC14y = 1449264253861378603094902135541032615454847633201512044204221948679369962626;
    
    uint256 constant IC15x = 14355805718345693478106828010462388527543463639996977446781200320377891671148;
    uint256 constant IC15y = 9791357672581126802086635486414070101285603972614289567690358742136129316878;
    
    uint256 constant IC16x = 16697974438509056670280285282876497182354475279822772110795998194470869384490;
    uint256 constant IC16y = 14978471257612173246602719005471399940990828211494780346048122975734576673973;
    
    uint256 constant IC17x = 18790182358353737457975956791602241029019155781154143204908751390420368101707;
    uint256 constant IC17y = 4616467909762223157089035917355235718491608454495353587732623205021771173126;
    
    uint256 constant IC18x = 13224090369329853498072163586269189884440310880884818617406511449352656377690;
    uint256 constant IC18y = 10630657996741353210222167169954718209803449747695362471153132777322998945278;
    
    uint256 constant IC19x = 370011588414048036036904901128343470692086889410885915122196856293122787633;
    uint256 constant IC19y = 8604565731568044134082166916981253897690069321178076210642991181011970827664;
    
    uint256 constant IC20x = 18644577375566038304376617420667355697325369407389053173560764066875450708269;
    uint256 constant IC20y = 7073957950500886584170976493852779405841077509637897349476111904895565847598;
    
    uint256 constant IC21x = 7641319293752164689724976322104887418427970335124167589671911058540829166386;
    uint256 constant IC21y = 12289108311733373499139173359525563904572234036264946528764111110873796149050;
    
    uint256 constant IC22x = 14495320058920266924759981360197867059902336913858659394247875062766357906951;
    uint256 constant IC22y = 185279975139156280404714685353955134119813950238524638852963349655192412636;
    
    uint256 constant IC23x = 11279073039454374617032672064378262618885984511441392046907431490755154544233;
    uint256 constant IC23y = 35953377899249493836711717488814834436090090546630257207322475926272872825;
    
    uint256 constant IC24x = 14263515708629325871018468426511059299160406911281575441723768743113123253568;
    uint256 constant IC24y = 8036062378262066125865505464336274004969747087712652478285420780146711622359;
    
    uint256 constant IC25x = 2026988878682925300491454498958519962190722589069781098080920187605736436539;
    uint256 constant IC25y = 12993469893243381554710767258337884327555720996232921661217076163205953207625;
    
    uint256 constant IC26x = 14741763470794449324047737530044464409862535502888413189601715688411368193498;
    uint256 constant IC26y = 7032429473776882348118809748797960438403723842725389900846356528640182037860;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[26] calldata _pubSignals) public returns (bool) {
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
                
                g1_mulAccC(_pVk, IC26x, IC26y, calldataload(add(pubSignals, 800)))
                

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
            
            checkField(calldataload(add(_pubSignals, 800)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
