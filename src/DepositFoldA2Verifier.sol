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

contract DepositFoldA2Verifier {
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
    uint256 constant deltax1 = 21539300226818846161946731140432230560836374016371063088250925426216561675091;
    uint256 constant deltax2 = 18340202788467224321820896037266727039000768154655540276274199712801865407370;
    uint256 constant deltay1 = 19141942046299350143656973070792408090926722868486776728732042304469899050310;
    uint256 constant deltay2 = 18292077487401141894319033659128203773842356436121537773579683354602630555324;

    
    uint256 constant IC0x = 6957278413512683277727103085510031693568986189765301612814445755869019976579;
    uint256 constant IC0y = 841255504099787852282897973609978460636744688939830405755166328881661394949;
    
    uint256 constant IC1x = 15443440584277847602685043608537104080587941733867437827323864483024934034119;
    uint256 constant IC1y = 7282933842748909607140587221725579266875667605249945825350655821829558810325;
    
    uint256 constant IC2x = 6584269610072371700125901356823776956838800908735628131809560445526101076227;
    uint256 constant IC2y = 18318461783586461233300982690935711056280722671808681216360454623425020523978;
    
    uint256 constant IC3x = 13916639174212013877984269024112665435763966189013689684484858145655317176566;
    uint256 constant IC3y = 8048420905957479894930344136849199678296302133543176004134682134291578480208;
    
    uint256 constant IC4x = 1740033077513952854897267624166099487460130454513761932286643068535474337926;
    uint256 constant IC4y = 1299519774811472508828306592572128528055471568925635714467405310462360719772;
    
    uint256 constant IC5x = 20604367331137621940481944020693023864261352217754412544489204478589349849603;
    uint256 constant IC5y = 17617004638943574636600107101520543417255421508173631376494574200073366611528;
    
    uint256 constant IC6x = 6504301927346419187788905998672820270388026582146717237004128048879182059029;
    uint256 constant IC6y = 14900648348431570026861765175369689396201810578549732731044205702115638874591;
    
    uint256 constant IC7x = 8704204073244668057661799555734129949927902129131902708050672044484973773381;
    uint256 constant IC7y = 1440691317903539618095746230391133068950735376265611642812038948857003737960;
    
    uint256 constant IC8x = 209259841645915472551621208461275667188134657183495212971073595465703357775;
    uint256 constant IC8y = 1576749315051000425627395742170431711182222764823098013484081867636180128013;
    
    uint256 constant IC9x = 6280843703077929072608198557179971956001779995109704007735992111268701250329;
    uint256 constant IC9y = 7528401925969833131289637858093994880148749174807158778298033897232007079036;
    
    uint256 constant IC10x = 4848583743031822783187914357116462657974775157883713653346358071279689871138;
    uint256 constant IC10y = 17915965495689438305971494847391129417916584867433616986610346000415122509395;
    
    uint256 constant IC11x = 17793114663372111063590484574815108612572113818266313855606063006605273424843;
    uint256 constant IC11y = 17413294993358517926361758466571296696361498511342279161947173642289761253483;
    
    uint256 constant IC12x = 21169285222369907434567986429058456257735568563357431680629427617032887204887;
    uint256 constant IC12y = 14893821513981028102480278893684923841672333511577106312472120662511518192475;
    
    uint256 constant IC13x = 10565938694545997616994037597572903065003321368439317348472577215579027413488;
    uint256 constant IC13y = 10466522390013176196795819157620783554072040506177872799594692732389387878463;
    
    uint256 constant IC14x = 1696044794135446665343477084444316455155364232013579565599898496731017595717;
    uint256 constant IC14y = 14231335943233379074126529286667967845314782949242856313545829455765793887179;
    
    uint256 constant IC15x = 10968994963655949727800023442666224296592006393706725567573466154443622524864;
    uint256 constant IC15y = 2006227008830538195778192955770406256478429758757981748107567789989505471310;
    
    uint256 constant IC16x = 8310914930757236403850492668968392178605902462865197612631746967887317051049;
    uint256 constant IC16y = 20559418156663382654767463832998954056848341486700602951827240362309892074281;
    
    uint256 constant IC17x = 367647423066200872183298721235119973241702223250893209089388823634698744571;
    uint256 constant IC17y = 9178843308267963420738767357789859690172581356229885651993161317664755897365;
    
    uint256 constant IC18x = 11695063291864821861765201434680149044232867943365650091746490467244781655029;
    uint256 constant IC18y = 14721674951471991641737253253938090112467864240093660619373923927196019619030;
    
    uint256 constant IC19x = 21667921637983008120878252226110262848103296520469352277807206078831970664360;
    uint256 constant IC19y = 19094067956386444362658947388457207786680059777971703717019352687982308651762;
    
    uint256 constant IC20x = 17196076618342968606911187986384978133900112087606986392975847440676584547106;
    uint256 constant IC20y = 1055448618673529118636388283576997830931638617418421927318531724743714735963;
    
    uint256 constant IC21x = 9052094919360241278836648122826280840251361118138847181508549124014585011285;
    uint256 constant IC21y = 17836573083873878051754081947815806514477804570716350440406548753412453214923;
    
    uint256 constant IC22x = 9883362786142191797312151867827296790079079091714657191503176975096898351356;
    uint256 constant IC22y = 744235584446079237914618496854302462882110421764782385103663505919070433004;
    
    uint256 constant IC23x = 18876289888158767991620586737471917291246709600750395194412803313633580874144;
    uint256 constant IC23y = 19649776080966023229459348377573536756015077653927506474685400186453377985040;
    
    uint256 constant IC24x = 12378016465449269451945574377626177092803770254683726199221073867718092230225;
    uint256 constant IC24y = 4621184791116890124097495435353749858532323803617326324721491192279927772745;
    
    uint256 constant IC25x = 18869611472477215966827256433686698451063184403928545418954517946809094512357;
    uint256 constant IC25y = 9857081628944212037971736185831300879262806096979679024036804008405092435288;
    
    uint256 constant IC26x = 3325431959097709340675466171221046507508170405014351589416614970019266621188;
    uint256 constant IC26y = 18514635022862326987363771855808760331982967321745730352851885531852575027047;
    
    uint256 constant IC27x = 16787533717482396688605757520609285399647796742075785753486877981815453921696;
    uint256 constant IC27y = 429272034778515045065863700332967414038303368296954777434369124860563026341;
    
    uint256 constant IC28x = 8000239554517014609565094234681034723071897561936721876799288970062588104047;
    uint256 constant IC28y = 5371860055179464504693877219970941743348041694278454183775316428099203155504;
    
    uint256 constant IC29x = 4349086414114614479189384622187666628784142586251829026319028884199096794707;
    uint256 constant IC29y = 20405837089690499306898770014420436692077800225589779021045242115426798967054;
    
    uint256 constant IC30x = 2484330092441307635764921531974041335895977996595208021343488378156794098936;
    uint256 constant IC30y = 18507227699495802304491326979015462428921242468962615990546242004210383877748;
    
    uint256 constant IC31x = 18046314214039906197499354829775338716433157166659513862335758871847461096916;
    uint256 constant IC31y = 9639269180113846961110455119372817428008760350212529263098150117611950184667;
    
    uint256 constant IC32x = 7256334149468226336643155092157162084861054864544547392831896108163994623920;
    uint256 constant IC32y = 16072374778140151533501235257425155719558100167353122861663635833020974751344;
    
    uint256 constant IC33x = 16128733048604858419368153500643714108427988392318685195952864527804606672104;
    uint256 constant IC33y = 10748023646465387396976472808106867784888011335838177441332600454567243501221;
    
    uint256 constant IC34x = 19511978648751815694798138479799867843725692242743041892064506469011844123367;
    uint256 constant IC34y = 21223918721326462314978562535306886634551269772537320662525717957685918534341;
    
    uint256 constant IC35x = 13580691126608951227036027844209722276072539799934781296900713861836370846512;
    uint256 constant IC35y = 9082649870691199212029674143201033712766902411629880472045034617363997311250;
    
    uint256 constant IC36x = 20032175470080701346634876804856809731749202620269172623164272334230570690648;
    uint256 constant IC36y = 19193190710869539797957253778219227309508494014889189074819722834359631098895;
    
    uint256 constant IC37x = 21739498295729406197663266805913694823136528963612039208272683458902149637023;
    uint256 constant IC37y = 20930056687435497445056551477555176572861292060310133373311387881972540847463;
    
    uint256 constant IC38x = 14871342648717498239457235065959995650313750171516744907380500222586788544927;
    uint256 constant IC38y = 5711796315759149164034471657334706777818730512460297669098607468510177183988;
    
    uint256 constant IC39x = 20291441386686937828196744890528120592329516944064498232001985670509361293066;
    uint256 constant IC39y = 14288824440087231437320022680982732032007077074623026443355684439434916908866;
    
    uint256 constant IC40x = 11349757046052844595486546364625731989167216782718773848734037166496807216522;
    uint256 constant IC40y = 20202180214445350618636075207650514193843473867249166477214955147498692668146;
    
    uint256 constant IC41x = 16684277466309983765859204248654125556987157749520611055273786588894702893585;
    uint256 constant IC41y = 8897951639533017732832367455450272608935569475495078583296511762601450457357;
    
    uint256 constant IC42x = 7404562892057649700206713813061505956410215753011411246876130341782433257646;
    uint256 constant IC42y = 8984179147184154549357059952511648984847182854430742087831661361561103498828;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[42] calldata _pubSignals) public returns (bool) {
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
                
                g1_mulAccC(_pVk, IC27x, IC27y, calldataload(add(pubSignals, 832)))
                
                g1_mulAccC(_pVk, IC28x, IC28y, calldataload(add(pubSignals, 864)))
                
                g1_mulAccC(_pVk, IC29x, IC29y, calldataload(add(pubSignals, 896)))
                
                g1_mulAccC(_pVk, IC30x, IC30y, calldataload(add(pubSignals, 928)))
                
                g1_mulAccC(_pVk, IC31x, IC31y, calldataload(add(pubSignals, 960)))
                
                g1_mulAccC(_pVk, IC32x, IC32y, calldataload(add(pubSignals, 992)))
                
                g1_mulAccC(_pVk, IC33x, IC33y, calldataload(add(pubSignals, 1024)))
                
                g1_mulAccC(_pVk, IC34x, IC34y, calldataload(add(pubSignals, 1056)))
                
                g1_mulAccC(_pVk, IC35x, IC35y, calldataload(add(pubSignals, 1088)))
                
                g1_mulAccC(_pVk, IC36x, IC36y, calldataload(add(pubSignals, 1120)))
                
                g1_mulAccC(_pVk, IC37x, IC37y, calldataload(add(pubSignals, 1152)))
                
                g1_mulAccC(_pVk, IC38x, IC38y, calldataload(add(pubSignals, 1184)))
                
                g1_mulAccC(_pVk, IC39x, IC39y, calldataload(add(pubSignals, 1216)))
                
                g1_mulAccC(_pVk, IC40x, IC40y, calldataload(add(pubSignals, 1248)))
                
                g1_mulAccC(_pVk, IC41x, IC41y, calldataload(add(pubSignals, 1280)))
                
                g1_mulAccC(_pVk, IC42x, IC42y, calldataload(add(pubSignals, 1312)))
                

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
            
            checkField(calldataload(add(_pubSignals, 640)))
            
            checkField(calldataload(add(_pubSignals, 672)))
            
            checkField(calldataload(add(_pubSignals, 704)))
            
            checkField(calldataload(add(_pubSignals, 736)))
            
            checkField(calldataload(add(_pubSignals, 768)))
            
            checkField(calldataload(add(_pubSignals, 800)))
            
            checkField(calldataload(add(_pubSignals, 832)))
            
            checkField(calldataload(add(_pubSignals, 864)))
            
            checkField(calldataload(add(_pubSignals, 896)))
            
            checkField(calldataload(add(_pubSignals, 928)))
            
            checkField(calldataload(add(_pubSignals, 960)))
            
            checkField(calldataload(add(_pubSignals, 992)))
            
            checkField(calldataload(add(_pubSignals, 1024)))
            
            checkField(calldataload(add(_pubSignals, 1056)))
            
            checkField(calldataload(add(_pubSignals, 1088)))
            
            checkField(calldataload(add(_pubSignals, 1120)))
            
            checkField(calldataload(add(_pubSignals, 1152)))
            
            checkField(calldataload(add(_pubSignals, 1184)))
            
            checkField(calldataload(add(_pubSignals, 1216)))
            
            checkField(calldataload(add(_pubSignals, 1248)))
            
            checkField(calldataload(add(_pubSignals, 1280)))
            
            checkField(calldataload(add(_pubSignals, 1312)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
