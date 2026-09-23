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

contract DepositFoldA1Verifier {
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
    uint256 constant deltax1 = 12828271146820433400959653883860646432333311026884603787352055986319048909191;
    uint256 constant deltax2 = 10577177750556705450049982956414130320069369074432768835355718111144639224608;
    uint256 constant deltay1 = 19374823457108834119871460717620367263306106787136444393846500274797047871986;
    uint256 constant deltay2 = 11191102077286667616782710696182782807868057063991395749251028075173899410881;

    
    uint256 constant IC0x = 9781733535249596851820251627462626383588647416191958034799783809358253876312;
    uint256 constant IC0y = 3746722050428139162427399283593918057734325244883011779771309664555461775761;
    
    uint256 constant IC1x = 8709392894568285436686524474336453527575918415166842132569721597926510021216;
    uint256 constant IC1y = 4242326991698209454408849094206309184798812611903336360574589485892572830428;
    
    uint256 constant IC2x = 14790582407996621998741264394729442734584850761401726380624007066337396359469;
    uint256 constant IC2y = 12933596671404588377297210809434587548260307228293848830356070635012996476479;
    
    uint256 constant IC3x = 8608198603691390102866068263494918497193352126252884820551103539043352430454;
    uint256 constant IC3y = 21246679028747460210524997253704939796934164160527343877949401796563245617891;
    
    uint256 constant IC4x = 21579439178239685769616820992777358584400845966491742305488216253352302649940;
    uint256 constant IC4y = 17211890764676793545510435765764025765130487883034441025352055984303329884502;
    
    uint256 constant IC5x = 2801362022610611419981934707723175633222280186304678985878931301053151333054;
    uint256 constant IC5y = 1984231210811839377956924918296058561319286041277650221592055636246283614718;
    
    uint256 constant IC6x = 5383330599042692288960701729588566818185664569076145645198324693075762881533;
    uint256 constant IC6y = 13719288202418814449708999211442244847689976041964516284459553760181791883969;
    
    uint256 constant IC7x = 6572017307495314346930407447877450442751699210430114466888740446427240602088;
    uint256 constant IC7y = 11442867092082108422102294561970180069969555645569565908242518939203637270236;
    
    uint256 constant IC8x = 11333930493263270344351101576281111997874981857996004619199541495116118890123;
    uint256 constant IC8y = 14987176023185393397447038818372082862707233808752242762785846213716393558702;
    
    uint256 constant IC9x = 9783380328105457329917396007750905920619124704889602317876585683218754515174;
    uint256 constant IC9y = 20637215426135382364667048271761440508551481003115578276144450142521184461358;
    
    uint256 constant IC10x = 8414444580731896859699195522676843307850656157153679148904278549313071871286;
    uint256 constant IC10y = 13921336616495821056379685202056236969274018027111768741833195973225955544388;
    
    uint256 constant IC11x = 2542680339245427897274349667477977953727118685761183587866454612877492948740;
    uint256 constant IC11y = 21127876311619910422952467050202424908531652621880432949856371412783756610362;
    
    uint256 constant IC12x = 12954182921367368078147268625735618958490835612036212821865564377548853079659;
    uint256 constant IC12y = 14744932917329989749093413628629575165276463165838584926539927094096545344280;
    
    uint256 constant IC13x = 15861566295960011355958178207798126737574136339980202516120219428033949316541;
    uint256 constant IC13y = 3812343968243326044400297514507935420830730665656059517386720467101059279135;
    
    uint256 constant IC14x = 11863965645557753581767182947309414508116570778822728772787307944317861267901;
    uint256 constant IC14y = 20635569366894885287919830330061168809399432427750627322645013513566904607953;
    
    uint256 constant IC15x = 19909985956436344676945786955102807930136387277669858889993947516784112494711;
    uint256 constant IC15y = 18930868103790561694178357916525515968431372631948679039598998145872397262286;
    
    uint256 constant IC16x = 10353683412535344950705332583523889716353390169092491014106281729712580452239;
    uint256 constant IC16y = 18838215352509618882640113066080168832087173124365946813388405443772707652316;
    
    uint256 constant IC17x = 4585810270702868898308081725900173384667759630871442679450555550203798433717;
    uint256 constant IC17y = 6882481737708903548513832390831265516765210955057063814025958314487448988683;
    
    uint256 constant IC18x = 12286807009228417610484466566122791715432229121376398835434040789876350081285;
    uint256 constant IC18y = 17781281232766861545708057393659065284455281392882050788811529912206979257826;
    
    uint256 constant IC19x = 4554900229980356952866073653818534620186166051814834694117902846379880100504;
    uint256 constant IC19y = 3529271564941441240672999492038930171137465352569478290786801904497110694932;
    
    uint256 constant IC20x = 9155952510650857229366171783297982806216046336138795247326361824852730747880;
    uint256 constant IC20y = 6279355227526384115056139955902995446231498261750405193132383264668774278474;
    
    uint256 constant IC21x = 20685479978190499152831942150563766083998048123668148578691439769785591560871;
    uint256 constant IC21y = 16815140353475865034751205948293463277375063745559857374560778941943965985956;
    
    uint256 constant IC22x = 17802249423623555042894387351177752945220942579321471160979759396484187588672;
    uint256 constant IC22y = 2302451403002399946613390692566662682708742723076033929600194354616419180595;
    
    uint256 constant IC23x = 5676958169414323762091297865201169253235976695327078908012878259391180488433;
    uint256 constant IC23y = 19077135990099199070886563568549008820481558083494422837020603876033880376802;
    
    uint256 constant IC24x = 17823495409974229196658625149422819653346185492725238813375983019125217686154;
    uint256 constant IC24y = 20122100010060586184638131650615178323637186236986607484432258162067713709967;
    
    uint256 constant IC25x = 6894094655443461182354327753594193903038470877616667421616435401984820807348;
    uint256 constant IC25y = 7966122124892220886442278278660983576368774989138839327880073299072560944885;
    
    uint256 constant IC26x = 3136840997670330229790269281628438307870661181006813329433553503249548116574;
    uint256 constant IC26y = 1871819245246041392196637089094042851395872130496109783589374950425712003209;
    
    uint256 constant IC27x = 18206395683526654582843569197360061787213724214980406604146761905017513057124;
    uint256 constant IC27y = 7008291250211737151462236417364388366367313233803491292545418228514166222540;
    
    uint256 constant IC28x = 5050774584667974806022054323601174956203946765063819313938603251157910859707;
    uint256 constant IC28y = 11710253683109457101253793602659337053446251592374658317369052801602841185480;
    
    uint256 constant IC29x = 5179327031694793634484781489023043699981379231768467373764191178360238994523;
    uint256 constant IC29y = 1128510527209294779630056039053073942311925345818881869074892973668544779698;
    
    uint256 constant IC30x = 12654595691605363819557424011770342062866073576001190235802552719254400123997;
    uint256 constant IC30y = 9913865786466888666785271519682275005602067370899589135896300954127416658801;
    
    uint256 constant IC31x = 3469271100577648832307413354042420601371118651810289796133339512615921268055;
    uint256 constant IC31y = 15186630492170026211932939230833657422961820691957434139305125540200956659377;
    
    uint256 constant IC32x = 9683936597820190468443568188178897220741305107397080803865723598590607172139;
    uint256 constant IC32y = 16230069174139634973104387134475480262621547111905457717504609913105691980201;
    
    uint256 constant IC33x = 5755273881997895660564922256729221796465466269412095795895912836761601530736;
    uint256 constant IC33y = 18590439065682136144431585421654899586255552832756391574984287171014122353887;
    
    uint256 constant IC34x = 5400949525145085418622109668319887361239968583055555850470055701836216956750;
    uint256 constant IC34y = 180340014978526144934003181851734896366804871841188041153260903907330020340;
    
    uint256 constant IC35x = 7169810378133666611199826082972218496822767916847640462034299595009519774471;
    uint256 constant IC35y = 3355739218185968490511919077747682322552735161282342856417377236957871556656;
    
    uint256 constant IC36x = 9474059498266929802344423321435755014360897255763950771950056368906362694697;
    uint256 constant IC36y = 2011215114285770671381269871118955494092063181814726104553408848625325365253;
    
    uint256 constant IC37x = 562134463534246397866056708992671823887206000223043313398543329869223640886;
    uint256 constant IC37y = 14514299173349546484912307021256855844567023016534070300128148405707925031989;
    
    uint256 constant IC38x = 5257287993578010331343168454231288561041860857295054449730112071324853972123;
    uint256 constant IC38y = 6434498928488156417642268015106120028884645838253061835348933131142747288884;
    
    uint256 constant IC39x = 15346009742741466737913940320212768854870270994058032872275686196519668044578;
    uint256 constant IC39y = 2633465768998975837014414486170822124997923621645502079398698176613829629288;
    
    uint256 constant IC40x = 7061770109428085580006668300050018733939527680687367050059549026694206341855;
    uint256 constant IC40y = 16982740170892395781290728603182416293136787339019684579157790125102450254900;
    
    uint256 constant IC41x = 5671160364055846815642354899138152501921668061298151972738747689857452668226;
    uint256 constant IC41y = 17931744371414561349846741894500991405062731264097172865986092335727462994267;
    
    uint256 constant IC42x = 9060483880040026437635590498009067772948443394481057808345210960591164429930;
    uint256 constant IC42y = 18197213740701894021486324366345766258057151864676258711344688203073860716287;
    
    uint256 constant IC43x = 21228333886625434729717032575072157707798115837797489826478912100953515815253;
    uint256 constant IC43y = 17815167458525597262312747753822381728263608136302450962782451668252033230126;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[43] calldata _pubSignals) public returns (bool) {
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
                
                g1_mulAccC(_pVk, IC43x, IC43y, calldataload(add(pubSignals, 1344)))
                

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
            
            checkField(calldataload(add(_pubSignals, 1344)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
