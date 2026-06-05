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

contract MintBatchA2N8Groth16Verifier {
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
    uint256 constant deltax1 = 913278065522857368393271754770332728176908653082577043188600834296004041724;
    uint256 constant deltax2 = 3125760211323129055002076590659990221734757078309951783386739411153470852827;
    uint256 constant deltay1 = 2289763051809604398080254417390306228041666149613634542414960238979977106042;
    uint256 constant deltay2 = 15301499368161171001910158363873383139227264967822874212673247208791386439622;

    
    uint256 constant IC0x = 216633395172832081749416521900769404956766920127271336680361503875029122920;
    uint256 constant IC0y = 13002912789381279527716350302161279670964999396571673343104006405929202129486;
    
    uint256 constant IC1x = 12075171219467473558713761733241214426141911212073656088614742783031289984439;
    uint256 constant IC1y = 1360853523808194927110025194900899272608565646656464703846158084586641916637;
    
    uint256 constant IC2x = 9346320358920644801465772263353647052613304611051914517086201512524873637187;
    uint256 constant IC2y = 16287375660202163213709308269620044609096470472953163384392451706346959056750;
    
    uint256 constant IC3x = 967053806572891125981883576288018335022702008236282873796032520375757951647;
    uint256 constant IC3y = 159836232622457881799843295071450784696423340383103348020801873386180198127;
    
    uint256 constant IC4x = 15029034556449921686101042356081184155465002121772692298043557455225517803657;
    uint256 constant IC4y = 10064081578627355849114205602434972920513441625250994396909986060565429234398;
    
    uint256 constant IC5x = 7293482068476091265254399110090390523043349970607097836174098604148619107556;
    uint256 constant IC5y = 8640307075728507524293388212097711982702738716517146233310840172969921078335;
    
    uint256 constant IC6x = 3913011635246703496383856497605350636928698991899254446700849615811905902345;
    uint256 constant IC6y = 16154530949474113279770257871461435850234275648905698469392721881574522119590;
    
    uint256 constant IC7x = 17424987669051984119370691970618887945591313086635783326311507349378423471464;
    uint256 constant IC7y = 17956733262354403164951926268698651491013329413881637068588206089289151684575;
    
    uint256 constant IC8x = 18189058286538623817928978522667077702726469269158688996488313964966492992641;
    uint256 constant IC8y = 14201267765635176969307888874181635456205004355740677603752157096416155735386;
    
    uint256 constant IC9x = 19995317711111263580174353895188057488301101277036913467008140613206522667685;
    uint256 constant IC9y = 11121102302983984054851483290327121465671937488047532661468146946047155555631;
    
    uint256 constant IC10x = 7391169055603496032104071816374792629963928867785770308352494230619214293416;
    uint256 constant IC10y = 6792797364959092614164043428276047215657253264938911639965167822641152006885;
    
    uint256 constant IC11x = 12382624691965804335280384514079532308126820822135206546105442221252969625947;
    uint256 constant IC11y = 11253196041630437339719300717289862227501780033166141114205041605888814768789;
    
    uint256 constant IC12x = 20204880238448311185560030002176461492475750760211233654469677246827342386704;
    uint256 constant IC12y = 16750162078731193670251089708265726402254736495867587395039232801019269585149;
    
    uint256 constant IC13x = 8447437565996352649762362270668926980398020684515027722251877134563584015764;
    uint256 constant IC13y = 9569762738753921423875356440477601015192165751265743151612726746332656203769;
    
    uint256 constant IC14x = 10352852935083661477189489948927258508651580574340449637505543583162449898923;
    uint256 constant IC14y = 1412617376956427682374455728296956863503648338271374111406392191899779447265;
    
    uint256 constant IC15x = 8311754533424188450631885130630240187696050064023275756437276992501888025271;
    uint256 constant IC15y = 2104461513588896958569196845888058838646499667418403672732522085195465031017;
    
    uint256 constant IC16x = 21328233845379464280774525481171913801716403568452386748739946646564843118819;
    uint256 constant IC16y = 13705659262639729115983221946366544637791973200032142023199530699830230447451;
    
    uint256 constant IC17x = 8556749057294265562308137333113560532136930742353538286490225252731931607094;
    uint256 constant IC17y = 3576697347361238693373488168433890963343271844251093224093974324508973785203;
    
    uint256 constant IC18x = 8728744182713563344898009524665478406973666313534297618849769454756039325010;
    uint256 constant IC18y = 2430181417162340565784538918999240660395878419038501741187837301542752610802;
    
    uint256 constant IC19x = 325903962252072282711000516189221607956868151334799569541580550909514536959;
    uint256 constant IC19y = 389867526512624690865768600581935695844619564950374415344751980204883784919;
    
    uint256 constant IC20x = 5013523048343401859295063247670210428244238252877625027615579142295533906579;
    uint256 constant IC20y = 12391256989012322726110084386893781870993179173442086107403216226281896965603;
    
    uint256 constant IC21x = 149558657273761835705851672714809765401386732466636922444003897768960554167;
    uint256 constant IC21y = 19296467952873522631272925484923231780535082507927472968042749188583823496037;
    
    uint256 constant IC22x = 3091895332810043621787541429189951258782519208044691694590863851530403625862;
    uint256 constant IC22y = 10947322303072758600308274103367725409294643443185427437166210789114197210120;
    
    uint256 constant IC23x = 16358284331452481938337294813380307614173287609580032548275361738261263443949;
    uint256 constant IC23y = 9382869356939328997683473580104905225738774150010335577107780447542144032855;
    
    uint256 constant IC24x = 8619717898999452858870394778531330455467084770608838541675725535018177180508;
    uint256 constant IC24y = 21393361926118324950099583886934948144280045706140773518215102427183099834816;
    
    uint256 constant IC25x = 17190163742783579777484007255795925925255935663400503460856047449177473810961;
    uint256 constant IC25y = 18888322575715042193290414172045185616341541482699715697675466556436172406329;
    
    uint256 constant IC26x = 4176203864074244510145283914456562541350626457306286218278496046677269492521;
    uint256 constant IC26y = 13251015843929101256770251626727500868707214352417303490396308917146473850346;
    
    uint256 constant IC27x = 5047157481138619202030734442357949144318378454985345718704928900682441361735;
    uint256 constant IC27y = 58469350243446737246602868262685128938672336722499432700619474794941730816;
    
    uint256 constant IC28x = 16710014419279314817970653298947588313436368063747771115636155676678003669126;
    uint256 constant IC28y = 20423926581902628074348323818409299288400780916235063666876123772217617251919;
    
    uint256 constant IC29x = 11889444629817545206260914782111320154708151277956468623489014325170914863910;
    uint256 constant IC29y = 21740818728200123946266357083585912796379164101122854180015663923256237551178;
    
    uint256 constant IC30x = 1254893041405787879112452237366404827760203734512797388491132915799937466976;
    uint256 constant IC30y = 19280861941427890951061467383529296485782276299256556709471183122168552460973;
    
    uint256 constant IC31x = 6947495950720779859511801139838725669493334509310225868212814654536982402003;
    uint256 constant IC31y = 7316656755865883887070676767593029550317510213163348878181383520499660789633;
    
    uint256 constant IC32x = 13467399436213317575094757693440285052815854767546975810988251306172910254919;
    uint256 constant IC32y = 21172669110869786143598042914284422372763479089834174485163002430144383335279;
    
    uint256 constant IC33x = 19292910329020355063426276399894902236959619126534163614686968000914802215937;
    uint256 constant IC33y = 12803735244165171473272041575850641845227700165697154093144242772421135897635;
    
    uint256 constant IC34x = 11161505322154616356504462139422686242363546261988381161724697498115946474905;
    uint256 constant IC34y = 4707029903630843198397196012712629653527389294561462073594366577232626560986;
    
    uint256 constant IC35x = 7481610897662739947189381683264779218640548733771911873106703628925080357389;
    uint256 constant IC35y = 19778343783431570449295587081430141769252609533548678460282503973807331843494;
    
    uint256 constant IC36x = 18668972966884587828751514531653375221920731944050460964910721497256308231242;
    uint256 constant IC36y = 3215679526754223002257302822397682797206909784907437097027578631818161148938;
    
    uint256 constant IC37x = 10995229153431925566266370199687327376542874618114260810784376781144253545105;
    uint256 constant IC37y = 10341760318641133737524594273474687325971395377913443367621026574874191225260;
    
    uint256 constant IC38x = 12766531577845470088015827621711944899239974425067437234518778532975195731808;
    uint256 constant IC38y = 16290105855547554599245111009446512441802764234287408685374997526826568614011;
    
    uint256 constant IC39x = 6029323735746248431966051228809486053711833308662005753560498977043806450489;
    uint256 constant IC39y = 21043042716103131730512006641734023927230474496010423034083278043951621149020;
    
    uint256 constant IC40x = 10398166144901095775815518486263726985935917116605848044302088520585340115990;
    uint256 constant IC40y = 13819708079099262281669344074830293644286530437735517846736017310195083011653;
    
    uint256 constant IC41x = 20186989414342235067929420196066707259418033471756974041945534916852065491045;
    uint256 constant IC41y = 5878852553158252884828875789555733043021608480714083316603889679377675389110;
    
    uint256 constant IC42x = 5530522478553383445968100861494307742657485306585219003892421044385475779910;
    uint256 constant IC42y = 19899456709266420670143685875432239462072615031241744409598361467145470445264;
    
    uint256 constant IC43x = 4393231445807896114460388634027009149551859499491695058524489661273652752853;
    uint256 constant IC43y = 21521562402787129909905392866402196132736825001774718296338128984217121232745;
    
    uint256 constant IC44x = 21259026522541069108888994162703102908746298741239969191827922515300686842527;
    uint256 constant IC44y = 20154786707723642736613366025692575639714180376971633199599779031813217017240;
    
 
    // Memory data
    uint16 constant pVk = 0;
    uint16 constant pPairing = 128;

    uint16 constant pLastMem = 896;

    function verifyProof(uint[2] calldata _pA, uint[2][2] calldata _pB, uint[2] calldata _pC, uint[44] calldata _pubSignals) public returns (bool) {
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
                
                g1_mulAccC(_pVk, IC44x, IC44y, calldataload(add(pubSignals, 1376)))
                

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
            
            checkField(calldataload(add(_pubSignals, 1376)))
            

            // Validate all evaluations
            let isValid := checkPairing(_pA, _pB, _pC, _pubSignals, pMem)

            mstore(0, isValid)
             return(0, 0x20)
         }
     }
 }
